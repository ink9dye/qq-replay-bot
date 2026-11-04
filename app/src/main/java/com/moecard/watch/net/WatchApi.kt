package com.moecard.watch.net

import com.moecard.watch.model.ConnectionInfo
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import kotlin.text.Charsets

/**
 * 观战调度服务端 HTTP 接口。所有请求带 Bearer 令牌。
 * 接口路径与 PC 端 watch-api 完全一致：/watch-api/status|heartbeat|claim|upload|report。
 */
class WatchApi(private val server: String, private val token: String) {

    private fun base(): String {
        var s = server.trim().removeSuffix("/")
        if (!s.startsWith("http://") && !s.startsWith("https://")) s = "http://$s"
        return s
    }

    private fun request(
        method: String,
        path: String,
        body: ByteArray? = null,
        headers: Map<String, String> = emptyMap(),
        connectTimeout: Int = 20_000,
        readTimeout: Int = 60_000,
    ): Pair<Int, ByteArray> {
        var last: Exception? = null
        repeat(2) { attempt ->
            try {
                return requestOnce(method, path, body, headers, connectTimeout, readTimeout)
            } catch (e: Exception) {
                last = e
                android.util.Log.w(
                    "MoeCardWatch",
                    "HTTP attempt ${attempt + 1} failed: ${e.javaClass.name}: ${e.message}",
                    e,
                )
            }
        }
        throw RuntimeException(
            "无法连接服务器：${last?.let { "${it.javaClass.simpleName}: ${it.message ?: "无详细信息"}" } ?: "未知错误"}",
            last,
        )
    }

    private fun requestOnce(
        method: String,
        path: String,
        body: ByteArray?,
        headers: Map<String, String>,
        @Suppress("UNUSED_PARAMETER") connectTimeout: Int,
        @Suppress("UNUSED_PARAMETER") readTimeout: Int,
    ): Pair<Int, ByteArray> {
        val bodyObj = when {
            body != null -> body.toRequestBody(null)
            method == "GET" -> null
            else -> ByteArray(0).toRequestBody(null)
        }
        val builder = Request.Builder().url("${base()}$path").method(method, bodyObj)
        builder.header("Authorization", "Bearer $token")
        builder.header("User-Agent", "MoeCardWatch/1.0.1")
        headers.forEach { (k, v) -> builder.header(k, v) }
        Http.client.newCall(builder.build()).execute().use { resp ->
            val data = resp.body?.bytes() ?: ByteArray(0)
            return resp.code to data
        }
    }

    private fun asJson(bytes: ByteArray): JSONObject = try {
        JSONObject(bytes.toString(Charsets.UTF_8))
    } catch (_: Exception) {
        JSONObject()
    }

    fun status(): ConnectionInfo {
        val (status, raw) = request("GET", "/watch-api/status")
        if (status == 401 || status == 403) throw AuthException("令牌无效或已过期")
        if (status != 200) throw RuntimeException("服务器状态错误（HTTP $status）")
        val json = asJson(raw)
        val workers = json.optJSONObject("workers") ?: JSONObject()
        var online = 0
        workers.keys().forEach { if (workers.optJSONObject(it)?.optBoolean("online") == true) online++ }
        return ConnectionInfo(
            onlineWorkers = online,
            pending = json.optInt("pending", 0),
            claimed = json.optInt("claimed", 0),
        )
    }

    fun heartbeat(worker: String, active: List<String>) {
        val payload = JSONObject().apply {
            put("worker", worker)
            if (active.isNotEmpty()) put("active", JSONArray(active))
        }
        request("POST", "/watch-api/heartbeat", payload.toString().toByteArray(Charsets.UTF_8), jsonHeader())
    }

    /** 长轮询领任务；204 表示暂无任务，返回 null。 */
    fun claim(worker: String, capacity: Int, waitSec: Double): JSONObject? {
        val payload = JSONObject().apply {
            put("worker", worker)
            put("capacity", capacity)
            put("wait", waitSec)
        }
        val (status, raw) = request(
            "POST",
            "/watch-api/claim",
            payload.toString().toByteArray(Charsets.UTF_8),
            jsonHeader(),
            readTimeout = maxOf(20_000, ((waitSec + 12) * 1000).toInt()),
        )
        if (status == 204) return null
        if (status == 401 || status == 403) throw AuthException("令牌无效或已过期")
        if (status != 200) throw RuntimeException("认领任务失败（HTTP $status）")
        val json = asJson(raw)
        val job = json.optJSONObject("job")
        return if (job != null && job.length() > 0) job else null
    }

    fun upload(jobId: String, name: String, bytes: ByteArray, players: List<String>, recordedAt: Double): Boolean {
        val query = "?job=${enc(jobId)}&name=${enc(name)}"
        // PC 端 watch.py: X-Players 使用 urllib.parse.quote URL 编码后放入头部（玩家名可能包含中文）
        val xPlayers = java.net.URLEncoder.encode(players.joinToString(","), "UTF-8")
        val headers = mapOf(
            "Content-Type" to "application/octet-stream",
            "X-Players" to xPlayers,
            "X-Recorded-At" to recordedAt.toString(),
        )
        val (status, _) = request("POST", "/watch-api/upload$query", bytes, headers, readTimeout = 90_000)
        return status == 200
    }

    fun report(worker: String, jobId: String, outcome: String, detail: Map<String, Any>) {
        val payload = JSONObject().apply {
            put("worker", worker)
            put("job", jobId)
            put("outcome", outcome)
            put("detail", JSONObject(detail))
        }
        request("POST", "/watch-api/report", payload.toString().toByteArray(Charsets.UTF_8), jsonHeader())
    }

    private fun jsonHeader(): Map<String, String> =
        mapOf("Content-Type" to "application/json; charset=utf-8")

    private fun enc(s: String): String = java.net.URLEncoder.encode(s, "UTF-8")

    companion object {
        /** 静态探测：验证服务器地址 + 令牌是否有效。 */
        fun testConnection(server: String, token: String): ConnectionInfo {
            return WatchApi(server, token).status()
        }
    }
}
