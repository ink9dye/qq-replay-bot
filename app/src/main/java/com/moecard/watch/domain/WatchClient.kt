package com.moecard.watch.domain

import com.moecard.watch.model.Account
import com.moecard.watch.model.AppConfig
import com.moecard.watch.model.ConnectionInfo
import com.moecard.watch.model.RoomInfo
import com.moecard.watch.model.WatchJob
import com.moecard.watch.net.AuthException
import com.moecard.watch.net.MoecardAuth
import com.moecard.watch.net.WatchApi
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
import java.io.File
import java.security.MessageDigest
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

    private val dateFmt = SimpleDateFormat("yyyyMMdd", Locale.US)
    private val nameFmt = SimpleDateFormat("HHmmss", Locale.US)

    /** 最近一次心跳时间，供上层看门狗判断客户端是否卡死。 */
    @Volatile private var lastHeartbeatAt = System.currentTimeMillis()

    /** 距离上次心跳多久（毫秒）。 */
    fun silentFor(): Long = System.currentTimeMillis() - lastHeartbeatAt

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
                api.heartbeat(config.clientName, activeJobIds())
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
        var saved = 0
        var uploaded = 0
        var bytes = 0L
        var lastFrames = 0
        var dir: File? = null
        val seenHashes = HashSet<String>()

        try {
            if (duplicate != null) {
                onLog("跳过重复房间：${room.title}（30 分钟内已录过）", "warn")
                updateJob(currentJob(jobId)?.copy(stage = "skipped", status = "重复跳过") ?: return)
                outcome = "noreplay"
            } else {
                val jobDir = replayDir(room, jobId)
                jobDir.mkdirs()
                dir = jobDir

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
                            onReplay = { blob ->
                                val sha = sha256(blob)
                                if (!seenHashes.add(sha)) {
                                    // 重连后服务器会把同一局录像重发一遍，跳过避免重复上传
                                    onLog("跳过重复录像片段（重连后重复下发）", "info")
                                } else {
                                    val path = saveReplay(jobDir, players, blob, sha)
                                    if (path != null) {
                                        saved++
                                        if (uploadWithRetry(jobId, jobDir, path, players, rounds = 2)) {
                                            uploaded++
                                            bytes += blob.size
                                            onLog("已上传录像：${path.name}", "success")
                                        } else {
                                            onLog("上传失败，已排队待网络恢复后补传：${path.name}", "warn")
                                        }
                                    }
                                }
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
            outcome = if (saved > 0) "saved" else "retry"
            throw e
        } catch (e: Exception) {
            outcome = "failed"
            onLog("任务 ${jobId.take(8)} 出错：${e.message}", "error")
        } finally {
            // 收尾全程兜底：网络抖动引发的异常绝不允许冒泡到协程作用域
            val stopping = !scope.isActive

            // 1) 释放槽位（必须成功）
            try {
                releaseSlot(slot)
            } catch (_: Exception) {
            }

            // 2) 收尾时再抢救一轮上传（停机中则跳过，文件留着下次/人工处理）
            var pendingLeft = 0
            val d = dir
            if (d != null) {
                if (!stopping) {
                    try {
                        uploadEvidence(d, jobId, players)
                        uploaded += flushUploads(jobId, rounds = 3)
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
            val worthRemembering = saved > 0 || (outcome == "noreplay" && lastFrames > 0)
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
                        append("保存 $saved 个小局，上传 $uploaded 个")
                        if (pendingLeft > 0) append("（$pendingLeft 个待补传）")
                    },
                    saved = saved, uploaded = uploaded, finishedAt = System.currentTimeMillis(),
                )
                if (finalJob != null) {
                    updateJob(finalJob)
                    try {
                        api.report(
                            config.clientName, jobId, outcome,
                            mapOf("saved" to saved, "uploaded" to uploaded, "bytes" to bytes, "pending" to pendingLeft),
                        )
                    } catch (e: CancellationException) {
                        // 忽略
                    } catch (e: Exception) {
                        onLog("结果上报失败：${e.message}（服务器的待认领队列会自动重新派发）", "warn")
                    }
                    bumpStats(outcome, saved, uploaded, bytes)
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

    private fun saveReplay(dir: File, players: List<String>, blob: ByteArray, sha: String): File? {
        if (blob.size < 32) return null
        dir.mkdirs()
        val digest = sha.take(8)
        val p1 = safePart(players.getOrNull(0) ?: "p1")
        val p2 = safePart(players.getOrNull(1) ?: "p2")
        val base = "${nameFmt.format(Date())}_${digest}_${p1}_vs_$p2"
        var path = File(dir, "$base.yrp")
        var i = 2
        while (path.exists()) {
            path = File(dir, "$base($i).yrp")
            i++
        }
        path.writeBytes(blob)
        // 小局胜负证据：用房间双方名 + digest（服务端据此出战报）
        val ev = File(dir, "${path.name}.evidence.json")
        ev.writeText(
            """{"digest":"$sha","players":${players.joinToString(prefix = "[", postfix = "]") { "\"$it\"" }},"recorded_at":${path.lastModified() / 1000.0}}""",
        )
        return path
    }

    private suspend fun uploadEvidence(dir: File, jobId: String, players: List<String>) {
        dir.listFiles { f -> f.name.endsWith(".yrp.evidence.json") }?.forEach { sidecar ->
            if (uploadWithRetry(jobId, dir, sidecar, players, rounds = 2)) {
                onLog("已上传胜负证据：${sidecar.name}", "success")
            } else {
                onLog("证据上传失败，已排队待补传：${sidecar.name}", "warn")
            }
        }
    }

    // ---------------------------------------------------------------- 上传 / 补传
    /** 上传一个文件；失败 [rounds] 次后进入补传队列，由心跳循环在网络恢复后继续重试。 */
    private suspend fun uploadWithRetry(
        jobId: String,
        dir: File,
        file: File,
        players: List<String>,
        rounds: Int,
    ): Boolean {
        val recordedAt = file.lastModified() / 1000.0
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
                pendingLock.withLock { pendingUploads.remove(p.key) }
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
                pendingLock.withLock { pendingUploads.remove(p.key) }
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

        /** 2s、4s、8s、16s，之后封顶 30s。 */
        fun backoffMs(attempt: Int): Long =
            (2_000L shl min(attempt - 1, 4)).coerceAtMost(30_000L)
    }
}
