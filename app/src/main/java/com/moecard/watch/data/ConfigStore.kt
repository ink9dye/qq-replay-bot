package com.moecard.watch.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.moecard.watch.model.Account
import com.moecard.watch.model.AppConfig
import org.json.JSONArray
import org.json.JSONObject

/**
 * 配置持久化：令牌与密码用 EncryptedSharedPreferences（Android 版 DPAPI 等价物）加密保存，
 * 只有本应用能解密；账号名、服务器地址等明文存储。
 */
class ConfigStore(context: Context) {
    private val masterKey = MasterKey.Builder(context)
        .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
        .build()

    private val prefs: SharedPreferences = EncryptedSharedPreferences.create(
        context,
        "watch_config",
        masterKey,
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    fun load(): AppConfig {
        val accounts = mutableListOf<Account>()
        try {
            val arr = JSONArray(prefs.getString("accounts", "[]"))
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i)
                accounts.add(Account(o.optString("name"), o.optString("password")))
            }
        } catch (_: Exception) {
        }
        return AppConfig(
            server = prefs.getString("server", "") ?: "",
            token = prefs.getString("token", "") ?: "",
            clientName = prefs.getString("clientName", "android-1") ?: "android-1",
            accounts = accounts,
            keepLocal = prefs.getBoolean("keepLocal", true),
            autoStart = prefs.getBoolean("autoStart", false),
        )
    }

    fun save(cfg: AppConfig) {
        val arr = JSONArray()
        cfg.accounts.forEach { a ->
            arr.put(JSONObject().put("name", a.name).put("password", a.password))
        }
        prefs.edit().apply {
            putString("server", cfg.server)
            putString("token", cfg.token)
            putString("clientName", cfg.clientName)
            putBoolean("keepLocal", cfg.keepLocal)
            putBoolean("autoStart", cfg.autoStart)
            putString("accounts", arr.toString())
            apply()
        }
    }

    /** 持久化“观战是否应处于运行状态”，供 watchdog / 开机自启判断。 */
    fun setWatchRunning(v: Boolean) {
        prefs.edit().putBoolean("watch_running", v).apply()
    }

    fun isWatchRunning(): Boolean = prefs.getBoolean("watch_running", false)
}
