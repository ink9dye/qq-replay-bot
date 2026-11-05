package com.moecard.watch.net

import java.util.concurrent.TimeUnit
import okhttp3.OkHttpClient

/** 全局共享的 OkHttp 客户端。替代系统 HttpURLConnection（在部分设备上有连接池 NPE bug）。 */
object Http {
    val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .writeTimeout(90, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()
}
