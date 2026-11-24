package com.moecard.watch.data

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * 打开一份加密 SharedPreferences，打不开时自愈而不是让应用崩溃。
 *
 * 典型场景：系统备份 / 换机迁移把加密后的 prefs 文件恢复回来了，但解密它的密钥在
 * Android Keystore 里，不会跟着备份走——旧实现在 Application.onCreate 里直接抛
 * AEADBadTagException / GeneralSecurityException，用户只能手动清除应用数据。
 *
 * 现在的处理：第一次打开失败 → 删掉这份 prefs 文件（里面的内容反正已经无法解密）→ 重建。
 * 代价是需要重新填写令牌和账号，但应用能正常启动。2.2.0 起 Manifest 也关闭了备份。
 */
object SecurePrefs {
    private const val TAG = "SecurePrefs"

    fun open(context: Context, name: String): SharedPreferences {
        val app = context.applicationContext
        return try {
            create(app, name)
        } catch (e: Exception) {
            Log.w(TAG, "加密配置 $name 无法打开（${e.javaClass.simpleName}），已重置", e)
            try {
                app.deleteSharedPreferences(name)
            } catch (_: Exception) {
            }
            create(app, name)
        }
    }

    private fun create(context: Context, name: String): SharedPreferences {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        return EncryptedSharedPreferences.create(
            context,
            name,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }
}
