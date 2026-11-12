package com.moecard.watch.query.net

import com.moecard.watch.net.AuthException
import com.moecard.watch.query.model.BuildData
import com.moecard.watch.query.model.CardDetail
import com.moecard.watch.query.model.PieData
import com.moecard.watch.query.model.QueryIdentity
import com.moecard.watch.query.model.ReplayData
import com.moecard.watch.query.model.RosterData
import com.moecard.watch.query.model.SearchResults
import com.moecard.watch.query.model.Subscription
import com.moecard.watch.query.model.TokenPair
import com.moecard.watch.query.model.parseBuild
import com.moecard.watch.query.model.parseCard
import com.moecard.watch.query.model.parseIdentity
import com.moecard.watch.query.model.parsePie
import com.moecard.watch.query.model.parseReplays
import com.moecard.watch.query.model.parseRoster
import com.moecard.watch.query.model.parseSearch
import com.moecard.watch.query.model.parseSubscriptions
import com.moecard.watch.query.model.parseTokenPair
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import kotlin.text.Charsets

/**
 * 自建查询 API 客户端（/query-api 前缀，路由与错误码对照 bot/query_api.py 核实）。
 *
 * 会话：login 用 QQ号 + 一次性激活码；access 默认 24h，refresh 默认 180d，
 * 刷新后旧的一对立即失效。所有读取接口带 Bearer access；遇到 401 单飞刷新后重试一次。
 */
class QueryApi(private val server: String, private val trustSelfSigned: Boolean) {

    private fun base(): String {
        var s = server.trim().removeSuffix("/")
        if (!s.startsWith("http://") && !s.startsWith("https://")) s = "https://$s"
        return s
    }

    // ---------- 底层请求 ----------

    private fun requestOnce(
        method: String,
        path: String,
        body: ByteArray?,
        headers: Map<String, String>,
    ): Pair<Int, ByteArray> {
        val bodyObj = when {
            body != null -> body.toRequestBody(null)
            method == "GET" -> null
            else -> ByteArray(0).toRequestBody(null)
        }
        val builder = Request.Builder().url("${base()}$path").method(method, bodyObj)
        builder.header("User-Agent", "MoeCardQuery/1.3")
        headers.forEach { (k, v) -> builder.header(k, v) }
        QueryHttp.client(trustSelfSigned).newCall(builder.build()).execute().use { resp ->
            return resp.code to (resp.body?.bytes() ?: ByteArray(0))
        }
    }

    /** 网络层重试一次（对齐服务端读超时 10s + 繁忙 429 的节奏）。 */
    private fun request(
        method: String,
        path: String,
        body: ByteArray? = null,
        token: String? = null,
    ): Pair<Int, ByteArray> {
        val headers = HashMap<String, String>()
        if (token != null) headers["Authorization"] = "Bearer $token"
        if (body != null) headers["Content-Type"] = "application/json; charset=utf-8"
        var last: Exception? = null
        repeat(2) {
            try {
                return requestOnce(method, path, body, headers)
            } catch (e: Exception) {
                last = e
            }
        }
        throw RuntimeException(
            "无法连接查询服务器：${last?.let { "${it.javaClass.simpleName}: ${it.message ?: "无详细信息"}" } ?: "未知错误"}",
            last,
        )
    }

    private fun asJson(bytes: ByteArray): JSONObject = try {
        JSONObject(bytes.toString(Charsets.UTF_8))
    } catch (_: Exception) {
        JSONObject()
    }

    /**
     * 带会话的请求：注入 Bearer；401 时加锁单飞刷新（并发请求只刷一次），
     * 换到新令牌后重试一次，仍 401 则视为会话失效。
     */
    private fun authed(method: String, path: String, body: ByteArray? = null): Pair<Int, ByteArray> {
        var token = QuerySession.currentAccessToken() ?: throw AuthException("请先登录")
        var resp = request(method, path, body, token)
        if (resp.first == 401) {
            synchronized(refreshLock) {
                val latest = QuerySession.currentAccessToken()
                if (latest != null && latest != token) {
                    // 等锁期间别的线程已经刷新过，直接用新令牌
                    token = latest
                } else {
                    doRefresh()
                    token = QuerySession.currentAccessToken() ?: throw AuthException("请先登录")
                }
            }
            resp = request(method, path, body, token)
            if (resp.first == 401) {
                QuerySession.expire()
                throw AuthException("登录已过期，请重新登录")
            }
        }
        return resp
    }

    private fun doRefresh() {
        val rt = QuerySession.currentRefreshToken() ?: throw AuthException("登录已过期，请重新登录")
        val payload = JSONObject().put("refresh_token", rt)
        val (status, raw) = request("POST", "/query-api/refresh", payload.toString().toByteArray(Charsets.UTF_8))
        if (status != 200) {
            QuerySession.expire()
            throw AuthException("登录已过期，请重新登录")
        }
        val pair = parseTokenPair(asJson(raw)) ?: run {
            QuerySession.expire()
            throw AuthException("登录已过期，请重新登录")
        }
        QuerySession.applyRefreshed(pair)
    }

    // ---------- 响应约定 ----------

    /** 读取接口：{"ok":true,"data":...}；失败抛带中文说明的异常。 */
    private fun dataOf(resp: Pair<Int, ByteArray>): Any? {
        val (status, raw) = resp
        if (status != 200) throw fail(status, raw)
        val json = asJson(raw)
        if (!json.optBoolean("ok", false)) {
            throw RuntimeException(json.optString("error", "请求失败"))
        }
        return if (json.has("data") && !json.isNull("data")) json.get("data") else null
    }

    /** 认证 / 订阅写入接口：直接返回字段（仍带 ok）。 */
    private fun rootOf(resp: Pair<Int, ByteArray>): JSONObject {
        val (status, raw) = resp
        if (status != 200) throw fail(status, raw)
        val json = asJson(raw)
        if (json.has("ok") && !json.optBoolean("ok", false)) {
            throw RuntimeException(json.optString("error", "请求失败"))
        }
        return json
    }

    private fun fail(status: Int, bytes: ByteArray): RuntimeException {
        val detail = asJson(bytes).optString("error", "")
        val base = when (status) {
            400 -> "请求无效"
            403 -> "没有权限（订阅写入需服务端开启写入且账号有写权限）"
            404 -> "未找到对应内容"
            409 -> "该账号已开通"
            429 -> "服务器繁忙，请稍后再试"
            503 -> "查询服务未启用或上游暂不可用"
            else -> "服务器错误（HTTP $status）"
        }
        val msg = if (detail.isNotBlank() && detail != base) "$base：$detail" else base
        if (status == 429 || status == 503) return BusyException(msg)
        return RuntimeException(msg)
    }

    /** 429（闸门满）/ 503（门户忙）单独成类，交给 queued() 自动重试。 */
    private class BusyException(msg: String) : RuntimeException(msg)

    /**
     * 全局串行闸门 + 繁忙重试：自建查询接口同一时刻只允许一条在途
     * （服务端并发上限 2，客户端更保守取 1，避免与门户自身计算抢闸门），
     * 遇 429/503 等 3 秒重试，最多 10 次。
     */
    private fun <T> queued(block: () -> T): T {
        gate.acquire()
        try {
            var attempt = 0
            while (true) {
                try {
                    return block()
                } catch (e: BusyException) {
                    attempt++
                    if (attempt >= 10) {
                        throw RuntimeException("服务器持续繁忙（已重试 $attempt 次），请稍后再试", e)
                    }
                    Thread.sleep(3000)
                }
            }
        } finally {
            gate.release()
        }
    }

    // ---------- 会话 ----------

    fun login(account: String, secret: String): TokenPair = queued {
        val payload = JSONObject()
            .put("account", account.trim())
            .put("secret", secret.trim())
        val resp = request("POST", "/query-api/login", payload.toString().toByteArray(Charsets.UTF_8))
        if (resp.first == 409) throw fail(409, resp.second)
        val json = rootOf(resp)
        parseTokenPair(json) ?: throw RuntimeException("服务器响应缺少令牌字段")
    }

    fun logout() {
        try {
            queued { authed("POST", "/query-api/logout") }
        } catch (e: AuthException) {
            // 会话已经失效，logout 无所谓成败
        }
    }

    /**
     * 用指定令牌吊销会话。
     *
     * 本地已经清空会话时 [logout] 拿不到令牌，所以退出登录要先把令牌抓出来再调这个。
     */
    fun revoke(accessToken: String) {
        queued { request("POST", "/query-api/logout", null, accessToken) }
    }

    fun me(): QueryIdentity = queued {
        val json = rootOf(authed("GET", "/query-api/me"))
        parseIdentity(json)
    }

    // ---------- 查询 ----------

    fun top100(): RosterData = queued {
        parseRoster(dataOf(authed("GET", "/query-api/top100")))
    }

    fun decks(player: String): BuildData = queued {
        parseBuild(dataOf(authed("GET", "/query-api/player/${enc(player)}/decks")))
            ?: throw RuntimeException("构筑数据为空")
    }

    fun replays(player: String): ReplayData = queued {
        parseReplays(dataOf(authed("GET", "/query-api/player/${enc(player)}/replays")))
    }

    fun pie(): PieData = queued {
        parsePie(dataOf(authed("GET", "/query-api/charts/pie")))
    }

    fun search(type: String, q: String): SearchResults = queued {
        require(type in setOf("player", "card", "deck")) { "type 必须是 player/card/deck" }
        val query = q.trim()
        require(query.isNotEmpty() && query.length <= 80) { "关键词长度需在 1~80 字之间" }
        parseSearch(type, dataOf(authed("GET", "/query-api/search?type=$type&q=${enc(query)}")))
    }

    fun card(code: Long): CardDetail = queued {
        require(code in 1..4294967295L) { "卡号超出范围" }
        val root = dataOf(authed("GET", "/query-api/card/$code"))
        val o = if (root is JSONObject) root else JSONObject()
        val details = if (o.has("details") && !o.isNull("details")) o.get("details") else null
        val metrics = if (o.has("metrics") && !o.isNull("metrics")) o.get("metrics") else null
        parseCard(code, details, metrics)
    }

    // ---------- 订阅（需要服务端 writes_enabled + 账号 writes） ----------

    fun subscriptions(): List<Subscription> = queued {
        parseSubscriptions(dataOf(authed("GET", "/query-api/subscriptions")))
    }

    fun subscribe(player: String, rankTop: Int?, notify: Boolean) = queued {
        val payload = JSONObject()
            .put("op", "subscribe")
            .put("player", player)
            .put("rank_top", rankTop ?: JSONObject.NULL)
            .put("notify", notify)
        rootOf(authed("POST", "/query-api/subscriptions", payload.toString().toByteArray(Charsets.UTF_8)))
    }

    fun unsubscribe(player: String) = queued {
        val payload = JSONObject()
            .put("op", "unsubscribe")
            .put("player", player)
        rootOf(authed("POST", "/query-api/subscriptions", payload.toString().toByteArray(Charsets.UTF_8)))
    }

    // ---------- 录像下载 ----------

    /** 录像走信任客户端直接拉字节（外链浏览器打开会有自签告警，应用内下载为主路径）。 */
    fun downloadBytes(url: String): ByteArray {
        val full = if (url.startsWith("http://") || url.startsWith("https://")) url else "${base()}$url"
        val req = Request.Builder().url(full)
            .header("User-Agent", "MoeCardQuery/1.3")
            .header("Authorization", "Bearer ${QuerySession.currentAccessToken() ?: ""}")
            .get()
            .build()
        var last: Exception? = null
        repeat(2) {
            try {
                QueryHttp.client(trustSelfSigned).newCall(req).execute().use { resp ->
                    if (resp.code != 200) throw RuntimeException("下载失败（HTTP ${resp.code}）")
                    return resp.body?.bytes() ?: ByteArray(0)
                }
            } catch (e: Exception) {
                last = e
            }
        }
        throw RuntimeException("下载失败：${last?.message ?: "网络错误"}", last)
    }

    // ---------- 工具 ----------

    private fun enc(s: String): String = URLEncoder.encode(s, "UTF-8").replace("+", "%20")

    companion object {
        private val refreshLock = Any()

        /** 全局串行闸门：跨 QueryApi 实例共享（登录、各屏共用一条队列）。 */
        private val gate = java.util.concurrent.Semaphore(1, true)
    }
}
