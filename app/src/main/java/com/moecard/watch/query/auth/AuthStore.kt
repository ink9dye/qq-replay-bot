package com.moecard.watch.query.auth

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.moecard.watch.query.model.TokenPair

/**
 * 查询端会话持久化：与观战配置（watch_config）分开的加密存储 query_auth。
 * 存查询令牌对、身份，以及查询服务器地址 / 自签信任开关。
 * 观战令牌、查询令牌、管理令牌三者用途分离，互不复用（见 QUERY_API.md）。
 */
class AuthStore(context: Context) {
    private val masterKey = MasterKey.Builder(context)
        .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
        .build()

    private val prefs: SharedPreferences = EncryptedSharedPreferences.create(
        context,
        "query_auth",
        masterKey,
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    fun loadSession(): TokenPair? {
        val token = prefs.getString("access_token", "") ?: ""
        val refresh = prefs.getString("refresh_token", "") ?: ""
        if (token.isBlank() || refresh.isBlank()) return null
        return TokenPair(
            token = token,
            refreshToken = refresh,
            accessExpiresAt = prefs.getLong("access_expires_at", 0L),
            refreshExpiresAt = prefs.getLong("refresh_expires_at", 0L),
            identity = prefs.getString("identity", "") ?: "",
            role = prefs.getString("role", "user") ?: "user",
            writes = prefs.getBoolean("writes", false),
        )
    }

    fun saveSession(session: TokenPair) {
        prefs.edit().apply {
            putString("access_token", session.token)
            putString("refresh_token", session.refreshToken)
            putLong("access_expires_at", session.accessExpiresAt)
            putLong("refresh_expires_at", session.refreshExpiresAt)
            putString("identity", session.identity)
            putString("role", session.role)
            putBoolean("writes", session.writes)
            apply()
        }
    }

    fun clearSession() {
        prefs.edit().apply {
            remove("access_token")
            remove("refresh_token")
            remove("access_expires_at")
            remove("refresh_expires_at")
            remove("identity")
            remove("role")
            remove("writes")
            apply()
        }
    }

    // 服务器与 TLS 信任是设备级设置，退出登录不清除
    fun loadServer(): String = prefs.getString("server", DEFAULT_SERVER) ?: DEFAULT_SERVER

    fun saveServer(server: String) {
        prefs.edit().putString("server", server).apply()
    }

    fun loadTrustSelfSigned(): Boolean = prefs.getBoolean("trust_self_signed", true)

    fun saveTrustSelfSigned(v: Boolean) {
        prefs.edit().putBoolean("trust_self_signed", v).apply()
    }

    // 录像库 /replay-api 是独立端口 + 独立 wt_ 令牌，单独存
    fun loadReplayServer(): String =
        prefs.getString("replay_server", "")?.takeIf { it.isNotBlank() }
            ?: defaultReplayServer(loadServer())

    fun saveReplayServer(server: String) {
        prefs.edit().putString("replay_server", server.trim()).apply()
    }

    fun loadReplayToken(): String = prefs.getString("replay_token", "") ?: ""

    fun saveReplayToken(token: String) {
        prefs.edit().putString("replay_token", token.trim()).apply()
    }

    // 首次登录的助战机器人引导：按登录身份记录，退出登录不清除（同一玩家只问一次）
    fun isWatchOnboarded(identity: String): Boolean =
        prefs.getBoolean("watch_onboarded_$identity", false)

    fun setWatchOnboarded(identity: String, value: Boolean) {
        prefs.edit().putBoolean("watch_onboarded_$identity", value).apply()
    }

    companion object {
        /** 部署约定：nginx 8443 TLS 入口（自签证书）。 */
        const val DEFAULT_SERVER = "https://101.201.108.188:8443"

        /** 录像库和查询服务同机，默认 http://<查询服务器主机>:8787。 */
        fun defaultReplayServer(queryServer: String): String = try {
            val host = java.net.URI(queryServer.trim()).host
            if (host.isNullOrBlank()) "http://101.201.108.188:8787" else "http://$host:8787"
        } catch (_: Exception) {
            "http://101.201.108.188:8787"
        }
    }
}
