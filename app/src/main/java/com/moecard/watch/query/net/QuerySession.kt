package com.moecard.watch.query.net

import android.content.Context
import com.moecard.watch.query.auth.AuthStore
import com.moecard.watch.query.model.TokenPair
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * 查询端全局会话：内存态令牌 + 加密持久化 + 服务器/TLS 配置。
 * 与观战（WatchManager / watch_config）完全隔离；App 启动时 init 一次。
 */
object QuerySession {

    private lateinit var store: AuthStore

    /** 当前会话；null 表示未登录（UI 显示登录页）。 */
    private val _session = MutableStateFlow<TokenPair?>(null)
    val session: StateFlow<TokenPair?> = _session.asStateFlow()

    @Volatile
    var server: String = AuthStore.DEFAULT_SERVER
        private set

    @Volatile
    var trustSelfSigned: Boolean = true
        private set

    /** 录像库（/replay-api，独立端口 + wt_ 令牌）。 */
    @Volatile
    var replayServer: String = ""
        private set

    @Volatile
    var replayToken: String = ""
        private set

    fun init(context: Context) {
        if (::store.isInitialized) return
        store = AuthStore(context.applicationContext)
        server = store.loadServer()
        trustSelfSigned = store.loadTrustSelfSigned()
        replayServer = store.loadReplayServer()
        replayToken = store.loadReplayToken().ifBlank {
            // 未单独填录像库令牌时，若观战配置里恰好是 wt_ 令牌则复用（同一身份）
            runCatching { com.moecard.watch.data.ConfigStore(context.applicationContext).load().token }
                .getOrDefault("")
                .trim()
                .takeIf { it.startsWith("wt_") }
                .orEmpty()
        }
        _session.value = store.loadSession()
    }

    fun currentAccessToken(): String? = _session.value?.token

    fun currentRefreshToken(): String? = _session.value?.refreshToken

    fun applyLogin(pair: TokenPair) {
        store.saveSession(pair)
        _session.value = pair
    }

    /** 刷新成功：保存新的一对（旧对已在服务端失效）。 */
    fun applyRefreshed(pair: TokenPair) {
        store.saveSession(pair)
        _session.value = pair
    }

    /** 会话失效：仅清令牌，服务器 / 信任设置保留。 */
    fun expire() {
        if (!::store.isInitialized) return
        store.clearSession()
        _session.value = null
    }

    /** 主动退出：本地立刻清会话，服务端的吊销放到后台线程。 */
    fun logout() {
        val token = currentAccessToken() ?: run {
            expire()
            return
        }
        val api = api()
        // 先清本地，UI 立即回到登录页
        expire()
        // 必须离开主线程：onClick 是主线程，直接发请求会触发 NetworkOnMainThreadException，
        // 异常被吞掉后服务端那对令牌就永远吊销不了。
        Thread {
            try {
                api.revoke(token)
            } catch (_: Exception) {
            }
        }.apply { isDaemon = true }.start()
    }

    fun api(): QueryApi = QueryApi(server, trustSelfSigned)

    fun replayApi(): ReplayApi = ReplayApi(replayServer, replayToken)

    fun setReplayServer(v: String) {
        val s = v.trim().removeSuffix("/")
        if (s.isBlank()) return
        replayServer = s
        if (::store.isInitialized) store.saveReplayServer(s)
    }

    fun setReplayToken(v: String) {
        replayToken = v.trim()
        if (::store.isInitialized) store.saveReplayToken(v)
    }

    fun setServer(v: String) {
        val s = v.trim().removeSuffix("/")
        if (s.isBlank()) return
        server = s
        if (::store.isInitialized) store.saveServer(s)
    }

    fun setTrustSelfSigned(v: Boolean) {
        trustSelfSigned = v
        if (::store.isInitialized) store.saveTrustSelfSigned(v)
    }
}
