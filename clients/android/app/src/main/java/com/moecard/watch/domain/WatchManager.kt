package com.moecard.watch.domain

import com.moecard.watch.model.AppConfig
import com.moecard.watch.model.ConnectionInfo
import com.moecard.watch.model.LogEntry
import com.moecard.watch.model.WatchJob
import com.moecard.watch.net.WatchApi
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 应用级单例，持有观战状态（连接概览 / 任务列表 / 日志 / 统计 / 运行状态），
 * 供 UI（ViewModel）观察与服务端后台运行共享同一份状态。
 *
 * 另带一层看门狗：观战客户端如果因为任何意外长时间没有心跳（连接循环/认领循环卡死、
 * 被异常打断），自动重启客户端，避免出现「界面显示运行中、实际已经不再观战」的情况。
 */
object WatchManager {
    private val _connectionInfo = MutableStateFlow<ConnectionInfo?>(null)
    val connectionInfo: StateFlow<ConnectionInfo?> = _connectionInfo.asStateFlow()

    private val _jobs = MutableStateFlow<List<WatchJob>>(emptyList())
    val jobs: StateFlow<List<WatchJob>> = _jobs.asStateFlow()

    private val _logs = MutableStateFlow<List<LogEntry>>(emptyList())
    val logs: StateFlow<List<LogEntry>> = _logs.asStateFlow()

    private val _stats = MutableStateFlow(WatchClient.Stats())
    val stats: StateFlow<WatchClient.Stats> = _stats.asStateFlow()

    private val _isRunning = MutableStateFlow(false)
    val isRunning: StateFlow<Boolean> = _isRunning.asStateFlow()

    private var client: WatchClient? = null
    private var lastConfig: AppConfig? = null
    private var watchdog: Job? = null
    private var dataDir: File? = null
    private val jobMap = LinkedHashMap<String, WatchJob>()
    private val scope = CoroutineScope(
        SupervisorJob() + Dispatchers.IO + CoroutineExceptionHandler { _, e ->
            if (e !is CancellationException) pushLog("后台异常：${e.message ?: e.javaClass.simpleName}", "error")
        },
    )
    private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.US)

    fun init(dataDir: File) {
        this.dataDir = dataDir
    }

    fun start(config: AppConfig) {
        if (_isRunning.value) return
        lastConfig = config
        launchClient()
    }

    private fun launchClient() {
        val cfg = lastConfig ?: return
        val dir = dataDir ?: return
        client = WatchClient(
            config = cfg,
            dataDir = dir,
            onLog = { msg, lvl -> pushLog(msg, lvl) },
            onJobUpdate = { upsertJob(it) },
            onConnectionInfo = { _connectionInfo.value = it },
            onStats = { _stats.value = it },
        )
        _isRunning.value = true
        client?.start()
        startWatchdog()
    }

    fun stop() {
        watchdog?.cancel()
        watchdog = null
        client?.stop()
        client = null
        _isRunning.value = false
        pushLog("观战客户端已停止", "info")
    }

    /** 网络变化时立即断开所有活跃连接以加速重连（任务本身会续看，不会被中断）。 */
    fun dropActiveConnections() {
        client?.dropActiveConnections()
    }

    /** 测试服务器连接与令牌。在 IO 线程调用。 */
    suspend fun testConnection(server: String, token: String): ConnectionInfo =
        WatchApi.testConnection(server, token)

    // ---------------------------------------------------------------- 看门狗
    private fun startWatchdog() {
        watchdog?.cancel()
        watchdog = scope.launch {
            while (isActive) {
                delay(WATCHDOG_TICK_MS)
                if (!_isRunning.value) continue
                val c = client ?: continue
                val silent = c.silentFor()
                if (silent > STALL_MS) {
                    pushLog("观战客户端已 ${silent / 1000} 秒无心跳，自动重启恢复…", "warn")
                    try {
                        c.stop()
                    } catch (_: Exception) {
                    }
                    client = null
                    launchClient()
                }
            }
        }
    }

    private fun pushLog(message: String, level: String) {
        val entry = LogEntry(System.currentTimeMillis(), message, level)
        val list = (_logs.value + entry).takeLast(500)
        _logs.value = list
    }

    private fun upsertJob(job: WatchJob) {
        synchronized(jobMap) {
            jobMap[job.id] = job
            _jobs.value = jobMap.values.sortedByDescending { it.startedAt }.take(200)
        }
    }

    private const val WATCHDOG_TICK_MS = 30_000L
    /** 心跳 10 秒一次；出现单次请求超时（最坏 2×60s）也远到不了这个阈值。 */
    private const val STALL_MS = 300_000L
}
