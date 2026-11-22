package com.moecard.watch.query.model

import android.content.Context
import org.json.JSONObject

/**
 * 卡组缩略图解析：卡组名 → 代表卡号 → CDN 卡图。
 *
 * 数据源有两层：
 *  - `deck_thumbs.json`：精选卡组名 → 代表卡（历史资产，精确匹配 + 前缀兜底）；
 *  - `series_thumbs.json`：系列/字段 → 代表卡（由 ygocdb 搜索生成，覆盖
 *    「米底乌斯无垢大艺术狱神耀圣」这类由多个系列拼出来的新卡组名）。
 *
 * 匹配顺序（从具体到宽泛）：
 *  1. 卡组名精确命中；
 *  2. 长度 ≥3 的最长前缀（避免「自奏」这种 2 字合集盖住「自奏圣乐」）；
 *  3. 卡组名中出现的系列（长度 ≥3，取出现位置最靠前的）；
 *  4. 长度 ≥2 的前缀；
 *  5. 任意系列。
 * 全部落空返回 null，UI 回落为带卡组名的色块。
 */
object DeckThumbs {

    @Volatile
    private var exact: Map<String, Long>? = null

    @Volatile
    private var series: Map<String, Long>? = null

    private fun load(context: Context, asset: String): Map<String, Long> {
        val map = HashMap<String, Long>()
        try {
            val text = context.assets.open(asset).bufferedReader().use { it.readText() }
            val json = JSONObject(text)
            json.keys().forEach { key ->
                val code = json.optLong(key, 0L)
                if (code > 0) map[key] = code
            }
        } catch (_: Exception) {
            // 资产缺失不致命，回落为无图色块
        }
        return map
    }

    private fun exactMap(context: Context): Map<String, Long> {
        exact?.let { return it }
        val loaded = load(context, "deck_thumbs.json")
        exact = loaded
        return loaded
    }

    private fun seriesMap(context: Context): Map<String, Long> {
        series?.let { return it }
        val loaded = load(context, "series_thumbs.json")
        series = loaded
        return loaded
    }

    /**
     * 卡组名里命中的字段，按名字中出现的先后排序；被更长字段包含的短字段丢弃（三幻魔优先于幻魔）。
     * 移植自服务端 bot/deck_pie.py::match_fields，保证客户端与统计网页的匹配口径一致。
     */
    internal fun matchFields(deckName: String, groups: Collection<String>): List<String> {
        val hits = groups.filter { it.isNotBlank() && it in deckName }
            .map { deckName.indexOf(it) to it }
            .sortedWith(compareByDescending<Pair<Int, String>> { it.second.length }.thenBy { it.first })
        val kept = ArrayList<Pair<Int, String>>()
        for (hit in hits) {
            if (kept.none { hit.second in it.second }) kept.add(hit)
        }
        return kept.sortedBy { it.first }.map { it.second }
    }

    fun codeFor(
        context: Context,
        deckName: String?,
        seriesTags: List<String> = emptyList(),
    ): Long? {
        if (deckName.isNullOrBlank()) return null
        val exact = exactMap(context)
        val seriesMap = seriesMap(context)
        val tags = seriesTags.map { it.trim() }.filter { it.isNotBlank() }

        exact[deckName]?.let { return it }

        // 3 字以上前缀，取最长
        exact.keys
            .filter { it.length >= 3 && deckName.startsWith(it) }
            .maxByOrNull { it.length }
            ?.let { return exact[it] }

        // 卡组名里命中的字段（先按长度降序，三幻魔优先于幻魔/混沌），取第一个有代表卡的
        val groups = LinkedHashSet<String>().apply {
            addAll(seriesMap.keys)
            addAll(tags)
        }
        matchFields(deckName, groups)
            .sortedByDescending { it.length }
            .firstOrNull { seriesMap.containsKey(it) }
            ?.let { return seriesMap[it] }

        // 2 字前缀 / 任意系列兜底
        exact.keys
            .filter { it.length >= 2 && deckName.startsWith(it) }
            .maxByOrNull { it.length }
            ?.let { return exact[it] }
        tags.firstOrNull { seriesMap.containsKey(it) }?.let { return seriesMap[it] }

        return null
    }

    fun cardImageUrl(code: Long): String = "$CDN_MAIN$code.jpg"

    /** momobako 没有的新卡（如 101307067）在 cdntx super-pre 上；404 时由 UI 切换。 */
    fun fallbackCardImageUrl(code: Long): String = "$CDN_FALLBACK$code.jpg"

    /** 卡组名 → 代表卡图 URL；全部匹配失败返回 null（UI 回落为色块）。 */
    fun thumbUrl(
        context: Context,
        deckName: String?,
        seriesTags: List<String> = emptyList(),
    ): String? = codeFor(context, deckName, seriesTags)?.let { cardImageUrl(it) }

    private const val CDN_MAIN = "https://cdn.233.momobako.com/ygopro/pics/"
    private const val CDN_FALLBACK = "https://cdntx.moecube.com/ygopro-super-pre/data/pics/"
}
