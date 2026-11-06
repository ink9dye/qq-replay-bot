package com.moecard.watch.query.net

import okhttp3.OkHttpClient
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

/**
 * 查询 API 专用 OkHttp 客户端工厂，与观战共享的 Http.client 完全独立。
 *
 * 服务器是自签证书 + IP 直连（https://101.201.108.188:8443），安卓默认不信任。
 * 按 QUERY_API.md「自签证书需要调用方显式信任」的约定，提供信任开关：
 * 开启时仅在这一个客户端上放宽校验（系统其他流量、观战流量都不受影响），
 * 后续若服务器换正式证书，在设置里关掉开关即可恢复严格校验。
 */
object QueryHttp {

    @Volatile
    private var cachedStrict: OkHttpClient? = null

    @Volatile
    private var cachedTrusting: OkHttpClient? = null

    fun client(trustSelfSigned: Boolean): OkHttpClient {
        if (!trustSelfSigned) {
            return cachedStrict ?: buildStrict().also { cachedStrict = it }
        }
        return cachedTrusting ?: buildTrusting().also { cachedTrusting = it }
    }

    private fun base(): OkHttpClient.Builder = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(40, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        // 查询接口读超时 10 秒 + 重试一次，对齐服务端资源边界
        .retryOnConnectionFailure(true)

    private fun buildStrict(): OkHttpClient = base().build()

    private fun buildTrusting(): OkHttpClient {
        val tm = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {}

            override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {}

            override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
        }
        val ssl = SSLContext.getInstance("TLS")
        ssl.init(null, arrayOf(tm), SecureRandom())
        return base()
            .sslSocketFactory(ssl.socketFactory, tm)
            .hostnameVerifier { _, _ -> true }
            .build()
    }
}
