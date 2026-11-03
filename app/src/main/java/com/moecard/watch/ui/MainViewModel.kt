package com.moecard.watch.ui

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.moecard.watch.data.ConfigStore
import com.moecard.watch.domain.WatchManager
import com.moecard.watch.model.Account
import com.moecard.watch.model.AppConfig
import com.moecard.watch.model.ConnectionInfo
import com.moecard.watch.service.WatchService
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class MainViewModel(app: Application) : AndroidViewModel(app) {
    private val configStore = ConfigStore(app)
    private val ctx = app.applicationContext

    private val _config = MutableStateFlow(configStore.load())
    val config: StateFlow<AppConfig> = _config.asStateFlow()

    val connectionInfo: StateFlow<ConnectionInfo?> = WatchManager.connectionInfo
    val jobs = WatchManager.jobs
    val logs = WatchManager.logs
    val stats = WatchManager.stats
    val isRunning = WatchManager.isRunning

    fun update(cfg: AppConfig) {
        _config.value = cfg
        configStore.save(cfg)
    }

    fun setServer(v: String) = update(_config.value.copy(server = v))
    fun setToken(v: String) = update(_config.value.copy(token = v))
    fun setClientName(v: String) = update(_config.value.copy(clientName = v))
    fun setKeepLocal(v: Boolean) = update(_config.value.copy(keepLocal = v))
    fun setAutoStart(v: Boolean) = update(_config.value.copy(autoStart = v))

    fun addAccount(name: String, password: String) {
        val cur = _config.value
        if (name.isBlank()) return
        update(cur.copy(accounts = cur.accounts + Account(name.trim(), password)))
    }

    fun removeAccount(index: Int) {
        val cur = _config.value
        update(cur.copy(accounts = cur.accounts.filterIndexed { i, _ -> i != index }))
    }

    fun testConnection(onResult: (Result<ConnectionInfo>) -> Unit) {
        viewModelScope.launch {
            try {
                val info = kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
                    WatchManager.testConnection(_config.value.server, _config.value.token)
                }
                onResult(Result.success(info))
            } catch (e: Exception) {
                Log.e("MoeCardWatch", "testConnection failed: server=${_config.value.server}", e)
                onResult(Result.failure(e))
            }
        }
    }

    fun startWatching() {
        configStore.save(_config.value)
        ctx.startForegroundService(
            android.content.Intent(ctx, WatchService::class.java)
                .setAction(WatchService.ACTION_START),
        )
    }

    fun stopWatching() {
        ctx.startService(
            android.content.Intent(ctx, WatchService::class.java)
                .setAction(WatchService.ACTION_STOP),
        )
    }
}
