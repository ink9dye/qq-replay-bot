package com.moecard.watch.net

import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.net.URLEncoder
import kotlin.text.Charsets

/**
 * 萌卡 / MoeCube 账号登录：signin 拿 token，再用 token 换 u16Secret（观战口令密钥）。
 * 与 watch.py 的 sign_in / fetch_u16_secret 对齐。
 */
object MoecardAuth {
    private const val ACCOUNTS_API = "https://api.moecube.com/accounts/"

    data class SignInResult(val secret: Int, val username: String)

    fun signIn(account: String, password: String): SignInResult {
        val form = "account=${enc(account)}&password=${enc(password)}"
        val resp = postJson(
            "$ACCOUNTS_API/signin",
            form.toByteArray(Charsets.UTF_8),
            mapOf("Content-Type" to "application/x-www-form-urlencoded", "User-Agent" to "Mozilla/5.0"),
        )
        val status = resp.status
        val payload = resp.json
        if (status == -1) throw RuntimeException("无法连接萌卡服务器：${resp.error ?: "网络不通（可能需配置代理）"}")
        if (status == 400) throw RuntimeException("萌卡账号或密码不正确（$account）")
        val token = payload.optString("token")
        if (status != 200 || token.isEmpty()) {
            throw RuntimeException("登录失败（HTTP $status）：${payload.optString("message").ifEmpty { payload.toString().take(200) }}")
        }
        val secret = fetchU16Secret(token)
        val user = payload.optJSONObject("user")
        val username = user?.optString("username")?.takeIf { it.isNotEmpty() } ?: account
        return SignInResult(secret, username)
    }

    fun fetchU16Secret(token: String): Int {
        val resp = getJson(
            "$ACCOUNTS_API/authUser",
            mapOf("Authorization" to "Bearer $token", "User-Agent" to "Mozilla/5.0"),
        )
        val status = resp.status
        val payload = resp.json
        if (status == -1) throw RuntimeException("无法连接萌卡服务器：${resp.error ?: "网络不通（可能需配置代理）"}")
        if (status == 401) throw RuntimeException("登录已过期，请重新填写账号")
        if (status != 200 || !payload.has("u16Secret")) {
            throw RuntimeException("刷新观战凭证失败（HTTP $status）：${payload.optString("message").ifEmpty { payload.toString().take(200) }}")
        }
        return payload.getInt("u16Secret")
    }

    private fun enc(s: String): String = URLEncoder.encode(s, "UTF-8")

    private fun postJson(url: String, body: ByteArray, headers: Map<String, String>): Response =
        request(url, "POST", body, headers)

    private fun getJson(url: String, headers: Map<String, String>): Response =
        request(url, "GET", null, headers)

    private data class Response(val json: JSONObject, val status: Int, val error: String? = null)

    private fun request(url: String, method: String, body: ByteArray?, headers: Map<String, String>): Response {
        return try {
            val bodyObj = if (body != null) body.toRequestBody(null) else null
            val builder = Request.Builder().url(url).method(method, bodyObj)
            headers.forEach { (k, v) -> builder.header(k, v) }
            Http.client.newCall(builder.build()).execute().use { resp ->
                val text = resp.body?.string() ?: "{}"
                val json = try {
                    JSONObject(text)
                } catch (_: Exception) {
                    JSONObject()
                }
                Response(json, resp.code, null)
            }
        } catch (e: Exception) {
            Response(JSONObject(), -1, e.message ?: e.javaClass.simpleName)
        }
    }
}
