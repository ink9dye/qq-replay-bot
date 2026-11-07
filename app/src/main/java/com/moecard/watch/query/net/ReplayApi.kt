package com.moecard.watch.query.net

import com.moecard.watch.query.model.ReplaySearch
import com.moecard.watch.query.model.parseReplaySearch
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.IOException
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

/**
 * 录像库 `/replay-api`（v2，按对局检索）。与查询 API 是两套东西：
 * 独立端口（默认 8787）、独立令牌（QQ 机器人「令牌分发」拿到的 wt_ 令牌）。
 *
 * 检索维度：player（任一方）/ opponent（对手）/ self_deck / opponent_deck / deck（任一方）/ 日期区间；
 * 返回按 session 聚合的对局，每局带双方视角（player、outcome、deck）与下载地址。
 */
class ReplayApi(private val base: String, private val token: String) {

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    private fun url(path: String): String = base.trimEnd('/') + path

    suspend fun matches(
        player: String? = null,
        opponent: String? = null,
        selfDeck: String? = null,
        opponentDeck: String? = null,
        deck: String? = null,
        from: String? = null,
        to: String? = null,
        page: Int = 1,
        pageSize: Int = 30,
    ): ReplaySearch = withContext(Dispatchers.IO) {
        val params = ArrayList<Pair<String, String>>()
        fun put(k: String, v: String?) {
            if (!v.isNullOrBlank()) params.add(k to v)
        }
        put("player", player)
        put("opponent", opponent)
        put("self_deck", selfDeck)
        put("opponent_deck", opponentDeck)
        put("deck", deck)
        put("from", from)
        put("to", to)
        put("page", page.toString())
        put("page_size", pageSize.toString())
        val qs = params.joinToString("&") { (k, v) ->
            "$k=" + URLEncoder.encode(v, "UTF-8")
        }
        val body = getJson("/replay-api/matches?$qs")
        val root = JSONObject(body)
        val data = root.optJSONObject("data") ?: root
        parseReplaySearch(data)
    }

    /** 下载单局 .yrp（path 用接口返回的 download 相对地址）。 */
    suspend fun downloadYrp(path: String): ByteArray = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url(url(path))
            .header("Authorization", "Bearer $token")
            .get()
            .build()
        client.newCall(req).execute().use { resp ->
            if (resp.code == 401 || resp.code == 403) throw IOException(NO_TOKEN)
            if (!resp.isSuccessful) throw IOException("录像下载失败（HTTP ${resp.code}）")
            resp.body?.bytes() ?: throw IOException("录像下载失败（空响应）")
        }
    }

    private suspend fun getJson(path: String): String = withContext(Dispatchers.IO) {
        if (token.isBlank()) throw IOException(NO_TOKEN)
        val req = Request.Builder()
            .url(url(path))
            .header("Authorization", "Bearer $token")
            .get()
            .build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            when {
                resp.code == 401 || resp.code == 403 -> throw IOException(NO_TOKEN)
                !resp.isSuccessful -> {
                    val err = runCatching { JSONObject(text).optString("error") }.getOrNull()
                    throw IOException(err?.takeIf { it.isNotBlank() } ?: "录像库请求失败（HTTP ${resp.code}）")
                }
                else -> text
            }
        }
    }

    companion object {
        const val NO_TOKEN = "录像库令牌缺失或已过期：QQ 给机器人发「令牌分发」拿 wt_ 令牌后填入"
    }
}
