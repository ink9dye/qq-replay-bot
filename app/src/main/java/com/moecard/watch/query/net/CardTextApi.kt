package com.moecard.watch.query.net

import com.moecard.watch.query.model.CardDetail
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * 卡片效果文本的快速通道：ygocdb 公共接口。
 *
 * 现网自建 `/query-api/card/<code>` 要 6~8 秒（服务端还要算 metrics），
 * 构筑页点开卡片的弹窗只读效果文本，先用 ygocdb（实测 0.3 秒左右）；
 * 失败时由调用方回退到自建接口（`QueryApi.card`）。
 */
object CardTextApi {

    private val cache = ConcurrentHashMap<Long, CardDetail>()

    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(4, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    fun cached(code: Long): CardDetail? = cache[code]

    /** 返回 null 表示 ygocdb 不可用/没有该卡，调用方应回退自建接口。 */
    fun fetch(code: Long): CardDetail? {
        cache[code]?.let { return it }
        val request = Request.Builder()
            .url("https://ygocdb.com/api/v0/card/$code")
            .header("User-Agent", "MoeCardQuery/1.3")
            .get()
            .build()
        return try {
            client.newCall(request).execute().use { resp ->
                if (resp.code != 200) return null
                val raw = resp.body?.string().orEmpty()
                if (raw.isBlank()) return null
                val json = JSONObject(raw)
                val text = json.optJSONObject("text") ?: return null
                val detail = CardDetail(
                    code = code,
                    name = text.optString("name").takeIf { it.isNotBlank() },
                    types = text.optString("types").takeIf { it.isNotBlank() },
                    desc = text.optString("desc").takeIf { it.isNotBlank() },
                    pdesc = text.optString("pdesc").takeIf { it.isNotBlank() },
                    imageUrl = null,
                    details = json,
                    metrics = null,
                )
                if (detail.desc == null && detail.pdesc == null) return null
                cache[code] = detail
                detail
            }
        } catch (_: Exception) {
            null
        }
    }
}
