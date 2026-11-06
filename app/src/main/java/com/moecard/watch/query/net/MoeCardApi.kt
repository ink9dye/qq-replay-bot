package com.moecard.watch.query.net

import com.moecard.watch.query.model.ArenaMatch
import com.moecard.watch.query.model.ArenaProfile
import com.moecard.watch.query.model.parseArenaHistory
import com.moecard.watch.query.model.parseArenaProfile
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.text.Charsets

/**
 * 萌卡官方竞技场接口（sapi.moecube.com:444，公开无鉴权）。
 * 用法对照服务端 arena_profile.py / player_history.py：
 *  - /ygopro/arena/user?username=X        → 当前 pt（即 DP）、排名、胜负场次
 *  - /ygopro/arena/history?username=X&type=1&page_num=50&page=N → 竞技对战历史
 *
 * 已知坑：history 的 page_num=100 会把接口卡死，必须拆成两页各取 50（服务端同款处理）。
 * 响应做 5 分钟内存缓存，避免玩家详情页几个区块反复拉同一份数据。
 */
class MoeCardApi {

    fun profile(username: String): ArenaProfile {
        val name = username.trim()
        return cache("profile:$name", TTL_SHORT) {
            val raw = get("/ygopro/arena/user?username=${enc(name)}")
            parseArenaProfile(raw) ?: throw RuntimeException("萌卡接口未返回该玩家的资料")
        }
    }

    /** 竞技对战历史（升序）。官方一次最多给 50 条，这里合并两页取最近 100 局。 */
    fun history(username: String): List<ArenaMatch> {
        val name = username.trim()
        return cache("history:$name", TTL_SHORT) {
            fetchPages(name, 1, 2)
        }
    }

    /**
     * 「加载更多」：从 [fromPage] 起再拉 [pages] 页（不缓存，与已加载列表
     * 由调用方按 start/end 去重合并）。
     */
    fun historyFrom(username: String, fromPage: Int, pages: Int = 2): List<ArenaMatch> =
        fetchPages(username.trim(), fromPage, pages)

    private fun fetchPages(name: String, fromPage: Int, pages: Int): List<ArenaMatch> {
        val merged = JSONObject()
        val rows = org.json.JSONArray()
        for (p in fromPage until fromPage + pages) {
            val raw = get("/ygopro/arena/history?username=${enc(name)}&type=1&page_num=50&page=$p")
            if (raw is JSONObject) {
                raw.optJSONArray("data")?.let { arr ->
                    for (i in 0 until arr.length()) rows.put(arr.get(i))
                }
            }
        }
        merged.put("data", rows)
        return parseArenaHistory(merged, name)
    }

    // ---------- 底层 ----------

    private fun get(path: String): Any? {
        var last: Exception? = null
        repeat(2) {
            try {
                val req = Request.Builder()
                    .url("$BASE$path")
                    .header("User-Agent", "MoeCardQuery/1.3")
                    .get()
                    .build()
                client.newCall(req).execute().use { resp ->
                    if (resp.code != 200) throw RuntimeException("萌卡接口错误（HTTP ${resp.code}）")
                    val text = resp.body?.string() ?: ""
                    if (text.isBlank()) throw RuntimeException("萌卡接口返回为空")
                    return when (text.trim().firstOrNull()) {
                        '[' -> org.json.JSONArray(text)
                        else -> JSONObject(text)
                    }
                }
            } catch (e: Exception) {
                last = e
            }
        }
        throw RuntimeException("无法访问萌卡接口：${last?.message ?: "网络错误"}", last)
    }

    private fun enc(s: String): String =
        java.net.URLEncoder.encode(s, "UTF-8").replace("+", "%20")

    companion object {
        private const val BASE = "https://sapi.moecube.com:444"
        private const val TTL_SHORT = 5 * 60 * 1000L // 对齐服务端 5 分钟缓存

        private val client: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()

        private val cache = HashMap<String, Pair<Long, Any>>()
        private val lock = Any()

        @Suppress("UNCHECKED_CAST")
        private fun <T> cache(key: String, ttlMs: Long, loader: () -> T): T {
            synchronized(lock) {
                val now = System.currentTimeMillis()
                cache.entries.removeIf { now - it.value.first > TTL_SHORT * 2 } // 顺带清过期项
                cache[key]?.let { (at, v) ->
                    if (now - at <= ttlMs) return v as T
                }
            }
            val v = loader()
            synchronized(lock) { cache[key] = System.currentTimeMillis() to (v as Any) }
            return v
        }

        /** 供下拉刷新等场景清空官方数据缓存。 */
        fun clearCache() {
            synchronized(lock) { cache.clear() }
        }
    }
}
