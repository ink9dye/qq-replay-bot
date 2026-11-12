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

    fun init(context: Context) {
        if (::store.isInitialized) return
        store = AuthStore(context.applicationContext)
        server = store.loadServer()
        trustSelfSigned = store.loadTrustSelfSigned()
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

    /** 主动退出：尽力通知服务端吊销当前一对，再清本地。 */
    fun logout() {
        val api = api()
        try {
            api.logout()
        } catch (_: Exception) {
        }
        expire()
    }

    fun api(): QueryApi = QueryApi(server, trustSelfSigned)

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
