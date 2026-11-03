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
import com.moecard.watch.protocol.Spectator
import com.moecard.watch.protocol.YgoProtocol
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
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

    private val api = WatchApi(config.server, config.token)
    private val scope = CoroutineScope(Dispatchers.IO)
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

    private val dateFmt = SimpleDateFormat("yyyyMMdd", Locale.US)
    private val nameFmt = SimpleDateFormat("HHmmss", Locale.US)

    fun start() {
        onLog("观战客户端已启动 · ${config.accounts.size} 个账号 · 客户端 ${config.clientName}", "success")
        emitStats()
        heartbeatLoop()
        claimLoop()
    }

    fun stop() {
        onLog("正在停止观战客户端…", "info")
        runBlocking { activeLock.withLock { activeSpectators.forEach { it.abort() } } }
        scope.coroutineContext[Job]?.cancel()
    }

    /**
     * 网络切换时调用：立即断开所有活跃观战连接，使其以“连接中断”快速结束，
     * 释放账号槽位后由认领循环重新连入（而非干等 30~120s 读超时）。
     */
    fun dropActiveConnections() {
        runBlocking {
            val list = activeLock.withLock { activeSpectators.toList() }
            if (list.isNotEmpty()) {
                onLog("检测到网络变化，主动断开 ${list.size} 路观战以加速重连", "warn")
                list.forEach { it.abort() }
            }
        }
    }

    // ---------------------------------------------------------------- 心跳 + 状态
    private fun heartbeatLoop() = scope.launch {
        while (isActive) {
            try {
                val info = api.status()
                onConnectionInfo(info)
                api.heartbeat(config.clientName, activeJobIds())
                onLog(
                    "服务器在线 ${info.onlineWorkers} 客户端 · 待认领 ${info.pending} · 本端活跃 ${activeCount()}/${config.capacity}",
                    "info",
                )
            } catch (e: AuthException) {
                onLog("心跳认证失败：${e.message}", "error")
            } catch (e: Exception) {
                onLog("心跳失败：${e.message}", "warn")
            }
            delay(10_000)
        }
    }

    // ---------------------------------------------------------------- 领任务循环
    private fun claimLoop() = scope.launch {
        var errors = 0
        while (isActive) {
            if (activeCount() >= config.capacity) {
                delay(500)
                continue
            }
            try {
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
                    delay(200)
                    continue
                }
                scope.launch { runJob(jobId, room, slot) }
            } catch (e: AuthException) {
                onLog("认领失败（令牌问题）：${e.message}", "error")
                break
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
        try {
            if (duplicate != null) {
                onLog("跳过重复房间：${room.title}（30 分钟内已录过）", "warn")
                updateJob(currentJob(jobId)?.copy(stage = "skipped", status = "重复跳过") ?: return)
                outcome = "noreplay"
            } else {
                ensureJoinGap()
                val secret = getSecret(slot)
                val action = if (room.joinId != null) 3 else 5
                val suffix = room.joinId ?: room.title
                val password = YgoProtocol.encodeMycardPassword(secret, action, suffix)
                if (password.length > 20) throw RuntimeException("房间标识过长，无法放入口令：$suffix")

                val dir = replayDir(room, jobId)
                dir.mkdirs()
                val spectator = Spectator(
                    host = room.host,
                    port = room.port,
                    version = room.version,
                    nickname = slot.nickname.ifEmpty { slot.account.name },
                    password = password,
                    onReplay = { blob ->
                        val path = saveReplay(dir, players, blob)
                        if (path != null) {
                            saved++
                            val rec = path.lastModified() / 1000.0
                            if (api.upload(jobId, path.name, blob, players, rec)) {
                                uploaded++
                                bytes += blob.size
                                onLog("已上传录像：${path.name}", "success")
                            } else {
                                onLog("上传失败：${path.name}", "warn")
                            }
                        }
                    },
                    onLog = onLog,
                    onConnected = {
                        val j = currentJob(jobId)
                        if (j != null) updateJob(j.copy(status = "观战中"))
                    },
                )
                activeLock.withLock { activeSpectators.add(spectator) }
                try {
                    val result = spectator.run()
                    outcome = result.outcome
                    uploadEvidence(dir, jobId, players)
                    if (!config.keepLocal) dir.deleteRecursively()
                } finally {
                    activeLock.withLock { activeSpectators.remove(spectator) }
                }
            }
        } catch (e: Exception) {
            outcome = "failed"
            onLog("任务 ${jobId.take(8)} 出错：${e.message}", "error")
        } finally {
            releaseSlot(slot)
            markRecent(room.key)
            val finalJob = currentJob(jobId)?.copy(
                stage = if (outcome == "saved") "done" else if (outcome == "noreplay" || outcome == "gone") "done" else "error",
                status = statusText(outcome),
                detail = "保存 $saved 个小局，上传 $uploaded 个",
                saved = saved, uploaded = uploaded, finishedAt = System.currentTimeMillis(),
            ) ?: return
            updateJob(finalJob)
            api.report(config.clientName, jobId, outcome, mapOf("saved" to saved, "uploaded" to uploaded, "bytes" to bytes))
            onLog("任务结束：${room.title} · ${statusText(outcome)}", if (outcome == "saved") "success" else "warn")
            bumpStats(outcome, saved, uploaded, bytes)
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

    private suspend fun ensureJoinGap() = joinLock.withLock {
        val wait = 20_000L + (0..5_000L).random()
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

    private fun saveReplay(dir: File, players: List<String>, blob: ByteArray): File? {
        if (blob.size < 32) return null
        dir.mkdirs()
        val sha = sha256(blob).take(8)
        val p1 = safePart(players.getOrNull(0) ?: "p1")
        val p2 = safePart(players.getOrNull(1) ?: "p2")
        val base = "${nameFmt.format(Date())}_${sha}_${p1}_vs_$p2"
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
            """{"digest":"${sha256(blob)}","players":${players.joinToString(prefix = "[", postfix = "]") { "\"$it\"" }},"recorded_at":${path.lastModified() / 1000.0}}""",
        )
        return path
    }

    private fun uploadEvidence(dir: File, jobId: String, players: List<String>) {
        dir.listFiles { f -> f.name.endsWith(".yrp.evidence.json") }?.forEach { sidecar ->
            try {
                val bytes = sidecar.readBytes()
                if (api.upload(jobId, sidecar.name, bytes, players, sidecar.lastModified() / 1000.0)) {
                    onLog("已上传胜负证据：${sidecar.name}", "success")
                }
            } catch (e: Exception) {
                onLog("证据上传失败：${e.message}", "warn")
            }
        }
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

    private suspend fun markRecent(roomKey: String) = recentLock.withLock {
        recentRooms[roomKey] = System.currentTimeMillis()
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
}
