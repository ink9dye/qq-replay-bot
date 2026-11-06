package com.moecard.watch.query.model

import android.content.Context
import org.json.JSONObject

/**
 * 卡组缩略图：本地资产 deck_thumbs.json（由 master 分支 reference_decks.json 用
 * 门户 deck_images 同款算法生成：卡组名 → 主卡组中总张数最高的代表卡号），
 * 卡图走 CDN（与服务端 card_search.py 同款双源：momobako 主、moecube 备）。
 */
object DeckThumbs {

    @Volatile
    private var map: Map<String, Long>? = null

    private fun load(context: Context): Map<String, Long> {
        map?.let { return it }
        val m = HashMap<String, Long>()
        try {
            val text = context.assets.open("deck_thumbs.json").bufferedReader().use { it.readText() }
            val json = JSONObject(text)
            json.keys().forEach { k ->
                val v = json.optLong(k, 0L)
                if (v > 0) m[k] = v
            }
        } catch (_: Exception) {
            // 资产缺失不致命，回落为无图色块
        }
        map = m
        return m
    }

    fun codeFor(context: Context, deckName: String?): Long? {
        if (deckName.isNullOrBlank()) return null
        return load(context)[deckName]
    }

    fun cardImageUrl(code: Long): String = "$CDN_MAIN$code.jpg"

    /** 卡组名 → 代表卡图 URL；没有映射时返回 null（UI 回落为色块）。 */
    fun thumbUrl(context: Context, deckName: String?): String? =
        codeFor(context, deckName)?.let { cardImageUrl(it) }

    private const val CDN_MAIN = "https://cdn.233.momobako.com/ygopro/pics/"
}
