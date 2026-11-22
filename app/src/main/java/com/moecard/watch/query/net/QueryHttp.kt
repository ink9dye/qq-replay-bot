package com.moecard.watch.query.net

import android.util.Log
import com.moecard.watch.BuildConfig
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Response
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
        .addInterceptor(DiagnosticInterceptor)

    /**
     * 调试构建的诊断日志：只记录方法、URL、状态码和错误响应体，
     * 不记录 Authorization，避免令牌进 logcat。发布构建整段跳过。
     */
    private object DiagnosticInterceptor : Interceptor {
        override fun intercept(chain: Interceptor.Chain): Response {
            val req = chain.request()
            if (!BuildConfig.DEBUG) return chain.proceed(req)
            val started = System.currentTimeMillis()
            val resp = try {
                chain.proceed(req)
            } catch (e: Exception) {
                Log.w(TAG, "${req.method} ${req.url} -> ${e.javaClass.simpleName}: ${e.message}")
                throw e
            }
            val cost = System.currentTimeMillis() - started
            Log.i(TAG, "${req.method} ${req.url} -> ${resp.code} (${cost}ms)")
            val path = req.url.encodedPath
            // 只对错误和订阅/录像两个排查热点记 body，避免把 top100/构筑整包写进 logcat
            if (resp.code >= 400 ||
                path.endsWith("/subscriptions") ||
                path.endsWith("/replays")
            ) {
                val body = try {
                    resp.peekBody(8192).string()
                } catch (_: Exception) {
                    ""
                }
                body.chunked(3000).forEachIndexed { index, part ->
                    Log.i(TAG, "  body[$index]: $part")
                }
            }
            return resp
        }
    }

    private const val TAG = "MoeQuery"

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
