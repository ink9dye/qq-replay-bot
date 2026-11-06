package com.moecard.watch

import android.app.Application
import com.moecard.watch.domain.WatchManager
import com.moecard.watch.query.net.QuerySession
import java.io.File

class WatchApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        // Android HttpURLConnection(okhttp) 连接池复用旧连接会偶发 NPE
        // （writeRequestHeaders on a null object reference），本 app 请求量小，直接禁用 keep-alive。
        System.setProperty("http.keepAlive", "false")
        // 数据目录放在应用私有外部存储，方便用文件管理器直接查看录像与日志
        val dir: File = getExternalFilesDir(null) ?: filesDir
        WatchManager.init(dir)
        // 查询端会话：恢复令牌对与服务器/TLS 配置（与观战配置分开存储）
        QuerySession.init(this)
    }
}
