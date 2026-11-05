package com.moecard.watch.domain

import com.moecard.watch.model.Account
import com.moecard.watch.model.AppConfig
import com.moecard.watch.model.ConnectionInfo
import com.moecard.watch.model.RoomInfo
import com.moecard.watch.model.WatchJob
import com.moecard.watch.net.AuthException
import com.moecard.watch.net.MoecardAuth
import com.moecard.watch.net.WatchApi
import com.moecard.watch.protocol.GameCapture
import com.moecard.watch.protocol.LiveEvidence
import com.moecard.watch.protocol.ReplayBatch
import com.moecard.watch.protocol.ReplayBlob
import com.moecard.watch.protocol.ReplayDecode
import com.moecard.watch.protocol.ReplayHeader
import com.moecard.watch.protocol.ReplaySplitter
import com.moecard.watch.protocol.SpectateResult
import com.moecard.watch.protocol.Spectator
import com.moecard.watch.protocol.YgoProtocol
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.security.SecureRandom
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.min

/**
 * 观战客户端核心：账号池 + 领任务循环 + 每账号独立登录/进房 + 观看保存上传。
 * 一个账号同时只进一间房；账号数 = 并发路数。纯 Kotlin + 协程，不依赖 Android。
 *
 * 抗网络抖动设计（关键）：
 *  1. 协程作用域用 SupervisorJob + 异常处理器：单个任务的异常不会连锁取消心跳/认领循环，
 *     否则一次网络抖动就会让整个客户端"静默死亡"（界面还显示运行中，实际不再观战）；
 *  2. 同一任务内自动重连：TCP 掉线/读超时不再直接结束任务，而是指数退避后重连同一间房，
 *     只有房间确实连不上（连续多次连接失败）或重连次数用尽才收尾；
 *  3. 上传失败进补传队列，网络恢复后由心跳循环（每 10s）自动补传，录像不会因为抖动丢失；
 *  4. 去重黑名单只对"确实录到东西"的房间生效，失败过的房间不会被打入 30 分钟冷宫。
 */
class WatchClient(
    private val config: AppConfig,
    private val dataDir: File,
    private val onLog: (String, String) -> Unit,
    private val onJobUpdate: (WatchJob) -> Unit,
    private val onConnectionInfo: (ConnectionInfo) -> Unit,
    private val onStats: (Stats) -> Unit,
) {
    data class Stats(
        val saved: Int = 0,
        val uploaded: Int = 0,
        val bytes: Long = 0,
        val jobs: Int = 0,
        val failed: Int = 0,
        val active: Int = 0,
        val capacity: Int = 1,
        val worker: String = "",
    )

    private data class AccountSlot(
        val account: Account,
        var state: String = "idle", // idle / busy / error
        var jobId: String? = null,
        var secret: Int = 0,
        var secretAt: Long = 0,
        var nickname: String = "",
        var error: String = "",
    )

    /** 待补传的录像/证据（网络恢复后重试）。 */
    private data class PendingUpload(
        val jobId: String,
        val dir: File,
        val name: String,
        val players: List<String>,
        val recordedAt: Double,
    ) {
        val key: String get() = dir.absolutePath + "/" + name
    }

    private val api = WatchApi(config.server, config.token)
    private val scope = CoroutineScope(
        SupervisorJob() + Dispatchers.IO + CoroutineExceptionHandler { _, e ->
            if (e !is CancellationException) {
                onLog("后台协程异常（已隔离，其余账号继续运行）：${e.message ?: e.javaClass.simpleName}", "error")
            }
        },
    )
    private val slots = config.accounts.map { AccountSlot(it) }
    private val slotsLock = Mutex()
    private val jobs = LinkedHashMap<String, WatchJob>()
    private val jobsLock = Mutex()
    private var stats = Stats(capacity = config.capacity, worker = config.clientName)
    private val statsLock = Mutex()
    private val recentRooms = LinkedHashMap<String, Long>()
    private val recentLock = Mutex()
    private var lastJoinAt = 0L
    private val joinLock = Mutex()
    private val activeSpectators = mutableSetOf<Spectator>()
    private val activeLock = Mutex()
    private val pendingUploads = LinkedHashMap<String, PendingUpload>()
    private val pendingLock = Mutex()
    /** 待补传记录落盘位置：应用重启后恢复，不把没传完的文件只留在内存里。 */
    private val pendingFile = File(dataDir, PENDING_FILE)
    /** 最近一次认领成功的时间：心跳在这之后的一小段宽限期内不声明 active，避免把刚认领的任务踢掉。 */
    @Volatile private var lastClaimAt = 0L

    private val dateFmt = SimpleDateFormat("yyyyMMdd", Locale.US)
    private val nameFmt = SimpleDateFormat("HHmmss", Locale.US)

    /** 最近一次心跳时间，供上层看门狗判断客户端是否卡死。 */
    @Volatile private var lastHeartbeatAt = System.currentTimeMillis()

    /** 距离上次心跳多久（毫秒）。 */
    fun silentFor(): Long = System.currentTimeMillis() - lastHeartbeatAt

    init {
        restorePendingUploads()
    }

    // ---------------------------------------------------------------- 待补传记录（落盘）
    /**
     * 恢复上次运行遗留的待补传文件。
     *
     * 只恢复文件还在、且不超过 [PENDING_TTL_MS] 的记录：太旧的录像一般已被服务器重新派发，
     * 再补传只会堆垃圾。记录本身损坏时当作没有，绝不影响主流程。
     */
    private fun restorePendingUploads() {
        if (!pendingFile.isFile) return
        try {
            val arr = JSONArray(pendingFile.readText())
            val now = System.currentTimeMillis()
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                val dirPath = o.optString("dir")
                val name = o.optString("name")
                if (dirPath.isBlank() || name.isBlank()) continue
                val dir = File(dirPath)
                if (!File(dir, name).isFile) continue
                if (now - o.optLong("saved_at", now) > PENDING_TTL_MS) continue
                val players = o.optJSONArray("players")?.let { a ->
                    (0 until a.length()).map { a.optString(it) }
                } ?: emptyList()
                val p = PendingUpload(
                    jobId = o.optString("job"),
                    dir = dir,
                    name = name,
                    players = players,
                    recordedAt = o.optDouble("recorded_at", 0.0),
                )
                pendingUploads[p.key] = p
            }
            if (pendingUploads.isNotEmpty()) onLog("已恢复 ${pendingUploads.size} 个待补传文件", "info")
        } catch (_: Exception) {
            try { pendingFile.delete() } catch (_: Exception) { }
        }
    }

    /** 调用方需持有 [pendingLock]。 */
    private fun persistPendingUploads() {
        try {
            val arr = JSONArray()
            pendingUploads.values.forEach { p ->
                arr.put(
                    JSONObject().apply {
                        put("job", p.jobId)
                        put("dir", p.dir.absolutePath)
                        put("name", p.name)
                        put("players", JSONArray(p.players))
                        put("recorded_at", p.recordedAt)
                        put("saved_at", System.currentTimeMillis())
                    },
                )
            }
            writeAtomic(pendingFile, arr.toString())
        } catch (_: Exception) {
            // 补传记录写不进去不影响本次补传（内存里还在）
        }
    }

    /** 临时文件 + 改名：避免进程被杀时留下半截 JSON。 */
    private fun writeAtomic(file: File, text: String) {
        file.parentFile?.mkdirs()
        val tmp = File(file.parentFile, "${file.name}.tmp")
        tmp.writeText(text)
        if (!tmp.renameTo(file)) {
            file.delete()
            if (!tmp.renameTo(file)) throw IOException("写文件失败：${file.path}")
        }
    }

    fun start() {
        onLog("观战客户端已启动 · ${config.accounts.size} 个账号 · 客户端 ${config.clientName}", "success")
        emitStats()
        heartbeatLoop()
        claimLoop()
        uploadLoop()
    }

    fun stop() {
        onLog("正在停止观战客户端…", "info")
        runBlocking { activeLock.withLock { activeSpectators.forEach { it.abort() } } }
        scope.coroutineContext[Job]?.cancel()
    }

    /**
     * 网络切换时调用：立即断开所有活跃观战连接，使其以“连接中断”快速结束，
     * 释放账号槽位后由认领循环重新连入（而非干等 30~120s 读超时）。
     * 断开后由 [runJob] 的重连循环负责续看，任务不会因此收尾。
     */
    fun dropActiveConnections() {
        runBlocking {
            val list = activeLock.withLock { activeSpectators.toList() }
            if (list.isNotEmpty()) {
                onLog("检测到网络变化，重连 ${list.size} 路观战（任务保持，不会中断）", "warn")
                list.forEach { it.abort() }
            }
        }
    }

    // ---------------------------------------------------------------- 心跳 + 状态
    private fun heartbeatLoop() = scope.launch {
        while (isActive) {
            lastHeartbeatAt = System.currentTimeMillis()
            try {
                val info = api.status()
                onConnectionInfo(info)
                onLog(
                    "服务器在线 ${info.onlineWorkers} 客户端 · 待认领 ${info.pending} · 本端活跃 ${activeCount()}/${config.capacity}",
                    "info",
                )
            } catch (e: CancellationException) {
                throw e
            } catch (e: AuthException) {
                onLog("心跳认证失败：${e.message}", "error")
            } catch (e: Exception) {
                onLog("心跳失败：${e.message}", "warn")
            }

            try {
                // 刚认领的 15 秒内不声明 active（服务端当“未知”，保留任务），
                // 之后如实上报——为空也要显式发 []，好让服务端清掉上一个进程遗留的租约。
                val claiming = System.currentTimeMillis() - lastClaimAt < CLAIM_GRACE_MS
                api.heartbeat(config.clientName, if (claiming) null else activeJobIds())
            } catch (e: CancellationException) {
                throw e
            } catch (e: AuthException) {
                onLog("心跳认证失败：${e.message}", "error")
            } catch (e: Exception) {
                onLog("心跳上报失败：${e.message}", "warn")
            }
            lastHeartbeatAt = System.currentTimeMillis()

            delay(10_000)
        }
    }

    // ---------------------------------------------------------------- 补传循环
    /**
     * 网络恢复后补传排队中的录像/证据。独立协程，避免上传的长耗时把心跳拖住
     * （心跳是上层看门狗判断“是否卡死”的依据）。
     */
    private fun uploadLoop() = scope.launch {
        while (isActive) {
            delay(15_000)
            try {
                if (pendingCount() > 0) {
                    val n = flushUploads(onlyJob = null, rounds = 1)
                    if (n > 0) onLog("已补传 $n 个录像文件", "success")
                }
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
            }
        }
    }

    // ---------------------------------------------------------------- 领任务循环
    private fun claimLoop() = scope.launch {
        var errors = 0
        while (isActive) {
            try {
                if (activeCount() >= config.capacity || !hasIdleSlot()) {
                    delay(500)
                    continue
                }
                val jobJson = api.claim(config.clientName, config.capacity, 10.0)
                errors = 0
                if (jobJson == null) {
                    delay(800)
                    continue
                }
                lastClaimAt = System.currentTimeMillis()
                val room = RoomInfo.fromJob(jobJson)
                val jobId = jobJson.optString("id")
                val slot = takeSlot(jobId)
                if (slot == null) {
                    // 认领到了却没有空槽：立刻退回，避免任务挂在服务器端却没人看
                    try {
                        api.report(config.clientName, jobId, "retry", mapOf("reason" to "no-free-slot"))
                    } catch (e: CancellationException) {
                        throw e
                    } catch (_: Exception) {
                    }
                    delay(500)
                    continue
                }
                scope.launch { runJob(jobId, room, slot) }
            } catch (e: CancellationException) {
                throw e
            } catch (e: AuthException) {
                // 令牌问题可能只是网络抖动导致的误判，不能因此永久停掉认领
                onLog("认领失败（令牌问题）：${e.message}，60 秒后重试", "error")
                delay(60_000)
            } catch (e: Exception) {
                errors++
                onLog("认领循环异常：${e.message}", "warn")
                delay(min(30_000, (1 shl min(errors, 4)) * 1000L))
            }
        }
    }

    private fun activeCount(): Int = runBlocking { slotsLock.withLock { slots.count { it.state == "busy" } } }

    private fun activeJobIds(): List<String> = runBlocking {
        slotsLock.withLock { slots.filter { it.state == "busy" }.mapNotNull { it.jobId } }
    }

    private suspend fun hasIdleSlot(): Boolean = slotsLock.withLock {
        slots.any { it.state == "idle" && it.jobId == null }
    }

    private suspend fun takeSlot(jobId: String): AccountSlot? = slotsLock.withLock {
        val slot = slots.firstOrNull { it.state == "idle" && it.jobId == null } ?: return@withLock null
        slot.state = "busy"
        slot.jobId = jobId
        slot
    }

    private suspend fun releaseSlot(slot: AccountSlot) = slotsLock.withLock {
        slot.state = "idle"
        slot.jobId = null
    }

    // ---------------------------------------------------------------- 单局观战
    private suspend fun runJob(jobId: String, room: RoomInfo, slot: AccountSlot) {
        val players = if (room.players.isNotEmpty()) room.players else room.seats.values.toList()
        updateJob(
            WatchJob(
                id = jobId, roomKey = room.key, title = room.title, players = players,
                account = slot.account.name, stage = "watching", status = "连接中",
            ),
        )
        val duplicate = checkDuplicate(room.key)
        var outcome = "failed"
        // 计数要在「读包协程」和「补传协程」之间共享，所以用加锁的小对象而不是局部 var。
        val tally = JobTally()
        var lastFrames = 0
        var dir: File? = null
        val seenHashes = HashSet<String>()
        // 小局胜负账本：建在任务级，断线重连后继续用同一份，不会把已收到的 MSG_WIN 丢掉。
        val evidence = LiveEvidence()
        // 本任务已落盘、尚未上传的各局；等胜负到了再连证据一起传。
        val savedGames = mutableListOf<SavedGame>()
        // 本场（一次观战任务）的标识：服务端用它把同一场的多局关联起来。
        val sessionId = randomHex(32)
        val jobDir = replayDir(room, jobId)

        /** 本任务下一个落盘序号（内部编号，只用于 previous_game 串联）。 */
        var savedSeq = 0

        /**
         * 上传一局：**先传旁证、再传录像**。
         *
         * 服务端是在收到 .yrp 的那一刻入库的，顺序反过来就会先入库成 unknown。
         * 「录像已上传」与「当前旁证已上传」分开记账：录像传过之后，迟到的胜负只补传旁证，
         * 不再整份录像重传；旁证内容没变（摘要相同）则一次都不重传。
         */
        suspend fun uploadGame(game: SavedGame) {
            val ev = game.evidenceFile
            val needReplay: Boolean
            val needEvidence: Boolean
            val evidenceSnapshot: String?
            val revision: Int
            synchronized(game) {
                needReplay = !game.uploaded
                evidenceSnapshot = game.evidenceHash
                revision = game.evidenceRevision
                needEvidence = ev.isFile && game.evidenceHash != null &&
                    game.evidenceUploadedHash != game.evidenceHash
            }
            if (!needReplay && !needEvidence) return
            val recordedAt = game.startedAt ?: (game.path.lastModified() / 1000.0)
            var evidenceOk = true
            if (needEvidence) {
                evidenceOk = uploadWithRetry(jobId, jobDir, ev, game.names, rounds = 2, recordedAt = recordedAt)
                if (evidenceOk) {
                    synchronized(game) { game.evidenceUploadedHash = evidenceSnapshot }
                    if (!needReplay) {
                        onLog("已补传更新后的旁证：${ev.name}（rev $revision）", "success")
                    }
                } else {
                    onLog("旁证上传失败，已排队补传：${ev.name}（录像不受影响）", "warn")
                }
            }
            if (!needReplay) return
            // 旁证没传上去也照样传录像：宁可先入库成未知胜负，也不能把录像丢在手机上。
            val ok = uploadWithRetry(jobId, jobDir, game.path, game.names, rounds = 2, recordedAt = recordedAt)
            if (!ok) {
                onLog("上传失败，已排队待网络恢复后补传：${game.path.name}", "warn")
                return
            }
            synchronized(game) { game.uploaded = true }
            tally.noteUploaded(game.size)
            onLog(
                "已上传录像：${game.path.name}" + when {
                    game.capture != null -> "（含小局胜负，${describeGameNo(game)}）"
                    evidenceOk -> "（本局无胜负证据）"
                    else -> "（旁证待补传）"
                },
                "success",
            )
        }

        /** 同一局只允许一个上传者，避免定时补传与收尾补传并发覆盖。 */
        suspend fun uploadIfIdle(game: SavedGame, waitMs: Long = 0): Boolean {
            val deadline = System.currentTimeMillis() + waitMs
            while (true) {
                val claimed = synchronized(game) {
                    if (game.uploading) {
                        false
                    } else {
                        game.uploading = true
                        true
                    }
                }
                if (claimed) break
                if (System.currentTimeMillis() >= deadline) return false
                delay(200)
            }
            try {
                uploadGame(game)
                return true
            } finally {
                synchronized(game) { game.uploading = false }
            }
        }

        // 到点就传：等胜负超时、或者本任务要收尾了，别把录像一直憋在本地。
        var flusher: Job? = null

        try {
            if (duplicate != null) {
                onLog("跳过重复房间：${room.title}（30 分钟内已录过）", "warn")
                updateJob(currentJob(jobId)?.copy(stage = "skipped", status = "重复跳过") ?: return)
                outcome = "noreplay"
            } else {
                jobDir.mkdirs()
                dir = jobDir
                flusher = scope.launch {
                    while (isActive) {
                        delay(FLUSH_TICK_MS)
                        val now = System.currentTimeMillis()
                        val due = synchronized(savedGames) {
                            savedGames.filter { g ->
                                (!g.uploaded && now >= g.deadlineAt) || (g.uploaded && g.evidenceDirty)
                            }
                        }
                        for (game in due) {
                            try {
                                uploadIfIdle(game)
                            } catch (e: CancellationException) {
                                throw e
                            } catch (e: Exception) {
                                onLog("补传异常：${e.message}", "warn")
                            }
                        }
                    }
                }

                var attempt = 0            // 重连次数
                var connectFails = 0       // 连续“连不上”次数
                while (true) {
                    // 首轮按全局错峰节奏进场；重连只需短暂间隔（房间还在，尽快接上）
                    ensureJoinGap(
                        minGapMs = if (attempt == 0) JOIN_GAP_MS else RECONNECT_GAP_MS,
                        jitterMs = if (attempt == 0) JOIN_JITTER_MS else RECONNECT_JITTER_MS,
                    )
                    // 口令依赖萌卡登录，抖动时登录本身也会失败，不能因此直接判死任务
                    val password = try {
                        preparePassword(slot, room)
                    } catch (e: CancellationException) {
                        throw e
                    } catch (e: Exception) {
                        onLog("获取观战口令失败：${e.message}", "warn")
                        null
                    }

                    val result: SpectateResult = if (password == null) {
                        SpectateResult("retry", connected = false)
                    } else {
                        val spectator = Spectator(
                            host = room.host,
                            port = room.port,
                            version = room.version,
                            nickname = slot.nickname.ifEmpty { slot.account.name },
                            password = password,
                            evidence = evidence,
                            onReplay = { batch ->
                                for ((index, item) in batch.blobs.withIndex()) {
                                    val sha = sha256(item.bytes)
                                    if (!seenHashes.add(sha)) {
                                        // 重连后服务器会把同一局录像重发一遍，跳过避免重复上传
                                        onLog("跳过重复录像片段（重连后重复下发）", "info")
                                        continue
                                    }
                                    val tail = index == batch.blobs.lastIndex
                                    val game = saveReplay(jobDir, players, item, sha, tail, sessionId, savedSeq + 1)
                                    if (game != null) {
                                        savedSeq = game.seq
                                        synchronized(savedGames) {
                                            // 同场上一局（有胜负时才会写进 previous_game）
                                            game.previous = savedGames.lastOrNull { it.seq == game.seq - 1 }
                                            savedGames.add(game)
                                        }
                                        writeEvidence(game)
                                        tally.noteSaved()
                                        // 已经有胜负、或者明显等不到胜负（补发的旧局）就把截止时间设成现在，
                                        // 由上传协程立刻处理——读包线程不做网络请求，避免阻塞协议接收。
                                        if (game.capture != null || !tail) {
                                            synchronized(game) { game.deadlineAt = System.currentTimeMillis() }
                                        }
                                    }
                                }
                            },
                            // 萌卡常在录像之后才推 MSG_WIN：补写旁证，由上传协程只补传旁证。
                            onWinCaptured = { capture ->
                                val game = attachLateCapture(savedGames, capture)
                                if (game != null) {
                                    evidence.consume(capture)
                                    onLog("终局消息已对回已落盘录像：${game.path.name}（${describeGameNo(game)}）", "success")
                                }
                                // 录像还没到属正常时序：胜负留在账本里，等 STOC_REPLAY 按局序号匹配。
                            },
                            onLog = onLog,
                            onConnected = {
                                val j = currentJob(jobId)
                                if (j != null && j.status != "观战中") updateJob(j.copy(status = "观战中"))
                            },
                        )
                        activeLock.withLock { activeSpectators.add(spectator) }
                        try {
                            spectator.run()
                        } catch (e: CancellationException) {
                            throw e
                        } catch (e: Exception) {
                            // 兜底：意外异常按“掉线”处理并交给重连循环，不直接判死这个任务
                            onLog("观战会话异常：${e.message ?: e.javaClass.simpleName}", "warn")
                            SpectateResult("retry", connected = true)
                        } finally {
                            activeLock.withLock { activeSpectators.remove(spectator) }
                        }
                    }

                    lastFrames = result.frames
                    outcome = result.outcome
                    if (outcome != "retry" && outcome != "lost") break

                    // ---- 连接中断 / 未完成：在同一任务内重连，不再直接放弃 ----
                    if (result.connected) {
                        connectFails = 0
                    } else {
                        connectFails++
                        if (connectFails >= MAX_CONNECT_FAILS) {
                            onLog("连续 $connectFails 次无法连入房间，判定房间已结束：${room.title}", "warn")
                            outcome = "gone"
                            break
                        }
                    }
                    attempt++
                    if (attempt > MAX_RECONNECTS) {
                        onLog("已重连 $MAX_RECONNECTS 次仍未完成，本任务收尾：${room.title}", "warn")
                        outcome = "retry"
                        break
                    }
                    val wait = backoffMs(attempt)
                    currentJob(jobId)?.let { updateJob(it.copy(status = "重连中（第 $attempt 次）")) }
                    onLog(
                        "观战连接中断（${if (result.connected) "房间仍在" else "暂时连不上"}），${wait / 1000} 秒后第 $attempt 次重连：${room.title}",
                        "warn",
                    )
                    delay(wait)
                }
            }
        } catch (e: CancellationException) {
            outcome = if (tally.saved > 0) "saved" else "retry"
            throw e
        } catch (e: Exception) {
            outcome = "failed"
            onLog("任务 ${jobId.take(8)} 出错：${e.message}", "error")
        } finally {
            // 收尾全程兜底：网络抖动引发的异常绝不允许冒泡到协程作用域
            val stopping = !scope.isActive

            // 0) 停掉定时补传，避免和收尾抢同一份文件
            try {
                flusher?.cancel()
            } catch (_: Exception) {
            }
            // 等它真的退出（在途的 HTTP 请求会以取消结束），否则收尾上传可能被"正在上传"标记挡住
            try {
                flusher?.join()
            } catch (_: Exception) {
            }

            // 1) 释放槽位（必须成功）
            try {
                releaseSlot(slot)
            } catch (_: Exception) {
            }

            // 2) 收尾：把还没传的各局（不管有没有等到胜负）连同已排队的补传一起清掉
            var pendingLeft = 0
            val d = dir
            if (d != null) {
                if (!stopping) {
                    try {
                        // 录像没传 或 旁证有新版本没被确认，都算"还没传完"
                        val remaining = synchronized(savedGames) {
                            savedGames.filter { !it.uploaded || it.evidenceDirty }
                        }
                        for (game in remaining) {
                            try {
                                uploadIfIdle(game, waitMs = 2_000)
                            } catch (e: CancellationException) {
                                throw e
                            } catch (e: Exception) {
                                onLog("收尾上传失败：${e.message}", "warn")
                            }
                        }
                        tally.addUploaded(flushUploads(jobId, rounds = 3))
                    } catch (e: CancellationException) {
                        // 停机导致的中断，忽略
                    } catch (_: Exception) {
                    }
                }
                pendingLeft = try { pendingCountFor(d) } catch (_: Exception) { 0 }
                // 还有没传完的就先别删，等心跳循环补传
                if (!config.keepLocal && !stopping && pendingLeft == 0) {
                    try { d.deleteRecursively() } catch (_: Exception) { }
                }
            }

            // 3) 去重：只有真录到东西才拉黑，否则房间会被白白跳过 30 分钟
            val worthRemembering = tally.saved > 0 || (outcome == "noreplay" && lastFrames > 0)
            try {
                markRecent(room.key, worthRemembering)
            } catch (_: Exception) {
            }

            // 4) 状态与上报
            try {
                val finalJob = currentJob(jobId)?.copy(
                    stage = if (outcome == "saved") "done" else if (outcome == "noreplay" || outcome == "gone") "done" else "error",
                    status = statusText(outcome),
                    detail = buildString {
                        append("保存 ${tally.saved} 个小局，上传 ${tally.uploaded} 个")
                        if (pendingLeft > 0) append("（$pendingLeft 个待补传）")
                    },
                    saved = tally.saved, uploaded = tally.uploaded, finishedAt = System.currentTimeMillis(),
                )
                if (finalJob != null) {
                    updateJob(finalJob)
                    try {
                        api.report(
                            config.clientName, jobId, outcome,
                            mapOf(
                                "saved" to tally.saved, "uploaded" to tally.uploaded,
                                "bytes" to tally.bytes, "pending" to pendingLeft,
                            ),
                        )
                    } catch (e: CancellationException) {
                        // 忽略
                    } catch (e: Exception) {
                        onLog("结果上报失败：${e.message}（服务器的待认领队列会自动重新派发）", "warn")
                    }
                    bumpStats(outcome, tally.saved, tally.uploaded, tally.bytes)
                }
            } catch (_: Exception) {
            }

            onLog(
                "任务结束：${room.title} · ${statusText(outcome)}",
                if (outcome == "saved") "success" else "warn",
            )
        }
    }

    private fun statusText(outcome: String): String = when (outcome) {
        "saved" -> "已完成"
        "noreplay" -> "无录像"
        "gone" -> "房间已结束"
        "retry" -> "未完成"
        "lost" -> "连接中断"
        else -> outcome
    }

    // ---------------------------------------------------------------- 账号凭据
    private suspend fun getSecret(slot: AccountSlot): Int {
        val now = System.currentTimeMillis()
        if (slot.secret != 0 && now - slot.secretAt < 150_000) return slot.secret
        val result = MoecardAuth.signIn(slot.account.name, slot.account.password)
        slot.secret = result.secret
        slot.secretAt = now
        slot.nickname = result.username
        onLog("萌卡账号已登录：${result.username}", "success")
        return result.secret
    }

    /** 生成进房口令（内部会按需重新登录萌卡，登录异常由调用方决定是否重试）。 */
    private suspend fun preparePassword(slot: AccountSlot, room: RoomInfo): String {
        val secret = getSecret(slot)
        val action = if (room.joinId != null) 3 else 5
        val suffix = room.joinId ?: room.title
        val password = YgoProtocol.encodeMycardPassword(secret, action, suffix)
        if (password.length > 20) throw RuntimeException("房间标识过长，无法放入口令：$suffix")
        return password
    }

    private suspend fun ensureJoinGap(minGapMs: Long, jitterMs: Long) = joinLock.withLock {
        val wait = minGapMs + (0..jitterMs).random()
        val gap = wait - (System.currentTimeMillis() - lastJoinAt)
        if (gap > 0) {
            onLog("错峰等待 ${gap / 1000} 秒后进场", "info")
            delay(gap)
        }
        lastJoinAt = System.currentTimeMillis()
    }

    // ---------------------------------------------------------------- 录像保存
    private fun replayDir(room: RoomInfo, jobId: String): File {
        val root = File(dataDir, "replays")
        val date = dateFmt.format(Date())
        return File(File(root, date), jobId)
    }

    /**
     * 已落盘的一局。录像与旁证各自记账：`uploaded` 只代表录像传上去了，
     * `evidenceUploadedHash` 才代表「这一版旁证」被服务端确认收到。
     */
    private data class SavedGame(
        val path: File,
        val digest: String,
        /** 本任务内的落盘顺序（内部编号，只用于 previous_game 串联）。 */
        val seq: Int,
        /** 本端局序号（MSG_START 计数），用来把晚到的胜负对回正确的录像。 */
        val serial: Int?,
        /** 有可靠依据的真实局次；没有依据时为 null，不猜。 */
        val gameNo: Int?,
        /** `announce` / `duel_start` / `unknown`。 */
        val gameNoSource: String,
        /** 是否是一帧 STOC_REPLAY 拆出来的最后一份（只有它可能是“刚打完”的那局）。 */
        val tail: Boolean,
        val names: List<String>,
        val startedAt: Double?,
        var association: String,
        val size: Int,
        /** 等到这个时刻还没有胜负就先传，别把录像憋死在本地。 */
        var deadlineAt: Long,
        /** 本场（一次观战任务）的标识，服务端用它把同一场的多局关联起来。 */
        val sessionId: String,
        var capture: GameCapture?,
        /** 上一局（同场），用来告诉服务端“下一局先后手由谁选”。 */
        var previous: SavedGame? = null,
        var uploaded: Boolean = false,
        /** 旁证文件当前内容的摘要，与最后一次上传成功的那份比对，决定是否需要补传。 */
        var evidenceHash: String? = null,
        var evidenceUploadedHash: String? = null,
        var evidenceRevision: Int = 0,
        /** 上传占用标记：同一局同时只允许一个上传者。 */
        var uploading: Boolean = false,
    ) {
        val evidenceFile: File get() = File(path.parentFile, "${path.name}.evidence.json")

        /** 旁证有新内容还没被服务端确认收到。 */
        val evidenceDirty: Boolean get() = evidenceHash != null && evidenceHash != evidenceUploadedHash
    }

    /** 局次的展示口径：有依据写第 N 局，没依据明说未知，不用内部序号冒充。 */
    private fun describeGameNo(game: SavedGame): String =
        game.gameNo?.let { "第 $it 局/${game.gameNoSource}" } ?: "局次未知"

    /** 单任务计数：读包协程和补传协程都会改，统一加锁。 */
    private class JobTally {
        private val lock = Any()
        private var savedCount = 0
        private var uploadedCount = 0
        private var byteCount = 0L

        val saved: Int get() = synchronized(lock) { savedCount }
        val uploaded: Int get() = synchronized(lock) { uploadedCount }
        val bytes: Long get() = synchronized(lock) { byteCount }

        fun noteSaved() {
            synchronized(lock) { savedCount++ }
        }

        fun noteUploaded(size: Int) {
            synchronized(lock) {
                uploadedCount++
                byteCount += size
            }
        }

        fun addUploaded(count: Int) {
            synchronized(lock) { uploadedCount += count }
        }
    }

    /**
     * 落盘一份录像并写小局旁证。
     *
     * 三处关键口径：
     *  1. 文件名和 `recorded_at` 用 .yrp 头里的**本局时间**，不是收到录像的时间；
     *  2. 旁证里的 `players` 用**录像内的顺序**（服务端逐字比对）；
     *  3. 这一局如果暂时没有胜负，也先把文件写下来，等 MSG_WIN 到了再补
     *     （见 [attachLateCapture]）。局次没有依据就留空，不写默认 1。
     */
    private fun saveReplay(
        dir: File,
        players: List<String>,
        item: ReplayBlob,
        sha: String,
        tail: Boolean,
        sessionId: String,
        seq: Int,
    ): SavedGame? {
        if (item.bytes.size < 32) return null
        dir.mkdirs()
        val digest = sha.take(8)
        // 录像里读出的双方顺序；读不出来才退回房间名单（此时不写 winner，免得配错人）
        val replayPlayers = ReplayDecode.playerNames(item.bytes)
        val names = replayPlayers ?: players
        val p1 = safePart(names.getOrNull(0) ?: "p1")
        val p2 = safePart(names.getOrNull(1) ?: "p2")
        val startedAt = replaySeconds(item.bytes)
        val stamp = startedAt?.let { nameFmt.format(Date((it * 1000).toLong())) } ?: nameFmt.format(Date())
        val base = "${stamp}_${digest}_${p1}_vs_$p2"
        var path = File(dir, "$base.yrp")
        var i = 2
        while (path.exists()) {
            path = File(dir, "$base($i).yrp")
            i++
        }
        path.writeBytes(item.bytes)
        val capture = item.capture.takeIf { replayPlayers != null }
        if (item.capture != null && capture == null) {
            onLog("未能读出录像内的玩家顺序，本局只存录像不带胜负（避免配错人）", "warn")
        }
        // 刚打完的那局（批尾）先等一会儿胜负；补发的旧局没胜负可等，立刻传。
        val waitWin = tail && capture == null
        val game = SavedGame(
            path = path,
            digest = sha,
            seq = seq,
            serial = item.serial,
            gameNo = item.gameNo,
            gameNoSource = item.gameNoSource,
            tail = tail,
            names = names,
            startedAt = startedAt,
            association = item.association,
            size = item.bytes.size,
            deadlineAt = System.currentTimeMillis() + if (waitWin) WIN_WAIT_MS else 0L,
            sessionId = sessionId,
            capture = capture,
        )
        val resultLabel = when {
            capture == null -> "无胜负"
            capture.winner in 0..1 -> "含胜负/座位 ${capture.winner + 1}"
            else -> "含小局平局"
        }
        onLog("已保存录像：${path.name} · $resultLabel · ${describeGameNo(game)}", "info")
        return game
    }

    /**
     * 写/重写某局的旁证文件（原子写，JSON 用 JSONObject 统一转义）。
     *
     * 字段与顺序完全按官方参考包 `android-ingest-reference` 的
     * `reference.yrp.evidence.json`：
     * `winner/reason/raw/received_at/source/events/captured_from_start/draw_capture_version/`
     * `game_no/association/digest/players/previous_game/session_id`。
     * 不一致的地方只在“没有真实证据时不填”：没有权威局次就不写 `game_no`，
     * 没从开局看起就不写 `events`/`draw_capture_version`，`captured_from_start` 一律如实写。
     */
    private fun writeEvidence(game: SavedGame): Boolean {
        val text = synchronized(game) {
            game.evidenceRevision += 1
            evidenceJson(game).toString()
        }
        return try {
            writeAtomic(game.evidenceFile, text)
            synchronized(game) { game.evidenceHash = sha256(text.toByteArray(Charsets.UTF_8)) }
            true
        } catch (e: Exception) {
            onLog("写旁证失败：${e.message}", "warn")
            false
        }
    }

    /** 调用方需持有 game 的锁（保证 revision 与内容一致）。 */
    private fun evidenceJson(game: SavedGame): JSONObject = JSONObject().apply {
        val capture = game.capture
        val complete = capture != null && capture.capturedFromStart && capture.events.isNotEmpty()
        if (capture != null) {
            put("winner", capture.winner)
            put("reason", capture.reason)
            put("raw", capture.raw)
            put("received_at", capture.receivedAtMs / 1000.0)
            put("source", "STOC_GAME_MSG/MSG_WIN")
            // 只有从开局看起、且消息流没被截断时才敢交 events；
            // 服务端也只有在 captured_from_start=true 时才会用它算先后手和用卡。
            if (complete) put("events", JSONArray(capture.events))
            put("captured_from_start", complete)
            if (complete) put("draw_capture_version", 1)
        }
        game.gameNo?.let { put("game_no", it) }
        put("association", game.association)
        put("digest", game.digest)
        put("players", JSONArray(game.names))
        put("previous_game", previousJson(game.previous) ?: JSONObject.NULL)
        put("session_id", game.sessionId)
    }

    /** 上一局的关联：服务端据此知道“下一局先后手由败方选”。信息不全时写 null，不猜。 */
    private fun previousJson(previous: SavedGame?): JSONObject? {
        val capture = previous?.capture ?: return null
        if (capture.winner !in 0..1) return null
        val loser = previous.names.getOrNull(1 - capture.winner) ?: return null
        return JSONObject().apply {
            put("digest", previous.digest)
            previous.gameNo?.let { put("game_no", it) }
            put("loser", loser)
        }
    }

    /**
     * 处理“录像先到、MSG_WIN 后到”。
     *
     * 不再挑“最近一个尾局且胜负为空”，而是按**本端局序号**对回唯一一份录像：
     *  - 序号相同（同一决斗内同一小局）才认；
     *  - 没有序号时退到已确认局次相同；
     *  - 都没有时，只有全任务恰好剩一局没证据（对应关系本身无歧义）才补；
     *  - 仍有多个候选就放弃，留未知 —— 错胜负比没有胜负更糟。
     *
     * @return 被补写的录像；没匹配上返回 null（此时胜负仍留在账本里等后续录像）。
     */
    private fun attachLateCapture(games: MutableList<SavedGame>, capture: GameCapture): SavedGame? {
        val candidates = synchronized(games) { games.filter { it.capture == null } }
        if (candidates.isEmpty()) return null
        val bySerial = if (capture.serial != null) {
            candidates.filter { it.serial != null && it.serial == capture.serial }
        } else {
            emptyList()
        }
        val byNo = if (capture.gameNo != null) {
            candidates.filter { it.gameNo != null && it.gameNo == capture.gameNo }
        } else {
            emptyList()
        }
        val distinct = (bySerial + byNo).distinct()
        // 有局序号时只认序号；只有双方都没有序号时，才允许“全任务只剩一局”这种无歧义兜底，
        // 绝不把已知序号的胜负塞给另一局（2026-11-05 实测就串到了第 2 局）。
        val target = distinct.singleOrNull()
            ?: candidates.singleOrNull()?.takeIf { it.serial == null || capture.serial == null }
            ?: return null

        val touched = ArrayList<SavedGame>()
        synchronized(games) {
            if (target.capture != null) return null   // 已被其他路径补过
            // 局次跟着这次落盘的结论走：有依据才有第 N 局，没依据保持 unknown
            target.capture = capture.numbered(target.gameNo, target.gameNoSource)
            if (target.association == "none") {
                target.association = "STOC_GAME_MSG/MSG_WIN + replay game serial（胜负晚于录像）"
            }
            touched.add(target)
            // 下一局如果已经落盘，把它的 previous_game 也补上（按落盘序号，不按猜测的局次）
            games.firstOrNull { it.seq == target.seq + 1 }?.let { next ->
                next.previous = target
                touched.add(next)
            }
        }
        touched.forEach { writeEvidence(it) }
        // 让上传协程立刻处理：录像已传过就只补传旁证，没传过就连旁证一起传。
        synchronized(target) { target.deadlineAt = System.currentTimeMillis() }
        return target
    }

    /** 该小局的时间（秒）：优先读 .yrp 头，读不到再用当前时间。 */
    private fun replaySeconds(blob: ByteArray): Double? =
        ReplayHeader.timestampSeconds(blob)?.toDouble()

    // ---------------------------------------------------------------- 上传 / 补传
    /** 上传一个文件；失败 [rounds] 次后进入补传队列，由心跳循环在网络恢复后继续重试。 */
    private suspend fun uploadWithRetry(
        jobId: String,
        dir: File,
        file: File,
        players: List<String>,
        rounds: Int,
        recordedAt: Double = file.lastModified() / 1000.0,
    ): Boolean {
        repeat(rounds) { i ->
            val ok = try {
                api.upload(jobId, file.name, file.readBytes(), players, recordedAt)
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                false
            }
            if (ok) return true
            if (i < rounds - 1) delay(1_000L * (i + 1))
        }
        pendingLock.withLock {
            val p = PendingUpload(jobId, dir, file.name, players, recordedAt)
            pendingUploads[p.key] = p
            persistPendingUploads()
        }
        return false
    }

    /** 重试排队中的上传，返回本次成功数量。[onlyJob] 非空时只处理该任务的文件。 */
    private suspend fun flushUploads(onlyJob: String?, rounds: Int): Int {
        val snapshot = pendingLock.withLock {
            pendingUploads.values.filter { onlyJob == null || it.jobId == onlyJob }
        }
        if (snapshot.isEmpty()) return 0
        var okCount = 0
        for (p in snapshot) {
            val f = File(p.dir, p.name)
            if (!f.exists()) {
                pendingLock.withLock {
                    pendingUploads.remove(p.key)
                    persistPendingUploads()
                }
                continue
            }
            var ok = false
            for (i in 0 until rounds) {
                ok = try {
                    api.upload(p.jobId, p.name, f.readBytes(), p.players, p.recordedAt)
                } catch (e: CancellationException) {
                    throw e
                } catch (_: Exception) {
                    false
                }
                if (ok) break
                if (i < rounds - 1) delay(1_000L * (i + 1))
            }
            if (ok) {
                pendingLock.withLock {
                    pendingUploads.remove(p.key)
                    persistPendingUploads()
                }
                okCount++
                onLog("补传成功：${p.name}", "success")
            }
        }
        return okCount
    }

    private suspend fun pendingCount(): Int = pendingLock.withLock { pendingUploads.size }

    private suspend fun pendingCountFor(dir: File): Int = pendingLock.withLock {
        pendingUploads.values.count { it.dir.absolutePath == dir.absolutePath }
    }

    /** 随机十六进制串（session_id 用）。 */
    private fun randomHex(length: Int): String {
        val bytes = ByteArray((length + 1) / 2)
        SecureRandom().nextBytes(bytes)
        return bytes.joinToString("") { "%02x".format(it) }.take(length)
    }

    private fun sha256(b: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }

    private fun safePart(text: String): String =
        text.replace(Regex("[\\\\/:*?\"<>|]"), "_").trim().trimEnd('.').take(20).ifEmpty { "unknown" }

    // ---------------------------------------------------------------- 去重
    private suspend fun checkDuplicate(roomKey: String): String? = recentLock.withLock {
        val now = System.currentTimeMillis()
        recentRooms.entries.removeIf { now - it.value > 30 * 60_000 }
        if (roomKey in recentRooms) roomKey else null
    }

    /** 只有确实录到录像的房间才记入去重表，避免网络抖动导致该房间 30 分钟内被跳过。 */
    private suspend fun markRecent(roomKey: String, worth: Boolean) = recentLock.withLock {
        if (worth) recentRooms[roomKey] = System.currentTimeMillis()
    }

    // ---------------------------------------------------------------- 状态维护
    private fun currentJob(jobId: String): WatchJob? = runBlocking { jobsLock.withLock { jobs[jobId] } }

    private fun updateJob(job: WatchJob) = runBlocking {
        jobsLock.withLock { jobs[job.id] = job }
        onJobUpdate(job)
        statsLock.withLock { stats = stats.copy(active = activeCount()) }
        onStats(stats)
    }

    private fun emitStats() = runBlocking { onStats(statsLock.withLock { stats }) }

    private suspend fun bumpStats(outcome: String, saved: Int, uploaded: Int, bytes: Long) = statsLock.withLock {
        stats.copy(
            saved = stats.saved + saved,
            uploaded = stats.uploaded + uploaded,
            bytes = stats.bytes + bytes,
            jobs = stats.jobs + 1,
            failed = stats.failed + if (outcome in setOf("failed", "lost", "retry")) 1 else 0,
            active = activeCount(),
        ).also { stats = it }
        onStats(stats)
    }

    private companion object {
        /** 首轮进场的全局错峰间隔（与原逻辑一致：20s + 0~5s 抖动）。 */
        const val JOIN_GAP_MS = 20_000L
        const val JOIN_JITTER_MS = 5_000L
        /** 断线重连的进场间隔：房间还在，尽快接上。 */
        const val RECONNECT_GAP_MS = 3_000L
        const val RECONNECT_JITTER_MS = 2_000L
        /** 同一任务最多重连次数（配合退避约 8 分钟）。 */
        const val MAX_RECONNECTS = 20
        /** 连续连不上多少次就判定房间已结束。 */
        const val MAX_CONNECT_FAILS = 3
        /** 认领成功后的心跳宽限期：这段时间不声明 active，避免把刚领到的任务当成“没在跑”清掉。 */
        const val CLAIM_GRACE_MS = 15_000L
        /** 刚打完那局最多等这么久的 MSG_WIN（实测约 25 秒到）；超时就先把录像传上去。 */
        const val WIN_WAIT_MS = 90_000L
        /** 定时补传的检查间隔。 */
        const val FLUSH_TICK_MS = 3_000L
        /** 待补传记录的落盘文件名与有效期（超过一天的一般已被服务器重新派发）。 */
        const val PENDING_FILE = "pending-uploads.json"
        const val PENDING_TTL_MS = 24 * 60 * 60 * 1000L

        /** 2s、4s、8s、16s，之后封顶 30s。 */
        fun backoffMs(attempt: Int): Long =
            (2_000L shl min(attempt - 1, 4)).coerceAtMost(30_000L)
    }
}
