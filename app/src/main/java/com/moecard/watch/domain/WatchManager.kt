package com.moecard.watch.domain

import com.moecard.watch.model.AppConfig
import com.moecard.watch.model.ConnectionInfo
import com.moecard.watch.model.LogEntry
import com.moecard.watch.model.WatchJob
import com.moecard.watch.net.WatchApi
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 应用级单例，持有观战状态（连接概览 / 任务列表 / 日志 / 统计 / 运行状态），
 * 供 UI（ViewModel）观察与服务端后台运行共享同一份状态。
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
    private var dataDir: File? = null
    private val jobMap = LinkedHashMap<String, WatchJob>()
    private val scope = CoroutineScope(Dispatchers.IO)
    private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.US)

    fun init(dataDir: File) {
        this.dataDir = dataDir
    }

    fun start(config: AppConfig) {
        if (_isRunning.value) return
        val dir = dataDir ?: return
        client = WatchClient(
            config = config,
            dataDir = dir,
            onLog = { msg, lvl -> pushLog(msg, lvl) },
            onJobUpdate = { upsertJob(it) },
            onConnectionInfo = { _connectionInfo.value = it },
            onStats = { _stats.value = it },
        )
        _isRunning.value = true
        client?.start()
    }

    fun stop() {
        client?.stop()
        client = null
        _isRunning.value = false
        pushLog("观战客户端已停止", "info")
    }

    /** 网络变化时立即断开所有活跃连接以加速重连。 */
    fun dropActiveConnections() {
        client?.dropActiveConnections()
    }

    /** 测试服务器连接与令牌。在 IO 线程调用。 */
    suspend fun testConnection(server: String, token: String): ConnectionInfo =
        WatchApi.testConnection(server, token)

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
}
