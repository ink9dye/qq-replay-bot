package com.moecard.watch.tools

import org.json.JSONObject

/**
 * 计算器用的卡片数据（YGOPro / ygocdb 的位域口径）。
 *
 * 字段直接来自 `https://ygocdb.com/api/v0/card/<卡号>` 的 `data` 段：
 * type / atk / def / level / race / attribute 都是 YGOPro cdb 里的原始整数。
 */
data class CardInfo(
    val code: Long,
    val name: String,
    val type: Long,
    /** -2 表示「?」。 */
    val atk: Int,
    /** -2 表示「?」；连接怪兽这里是连接标记位，不是守备力。 */
    val def: Int,
    /** 低 8 位是等级 / 阶级 / 连接值；灵摆怪兽的高位是刻度。 */
    val rawLevel: Long,
    val race: Long,
    val attribute: Long,
) {
    val isMonster: Boolean get() = type and TYPE_MONSTER != 0L
    val isFusion: Boolean get() = type and TYPE_FUSION != 0L
    val isSynchro: Boolean get() = type and TYPE_SYNCHRO != 0L
    val isXyz: Boolean get() = type and TYPE_XYZ != 0L
    val isLink: Boolean get() = type and TYPE_LINK != 0L

    /** 额外卡组怪兽（融合 / 同调 / 超量 / 连接）。 */
    val isExtraMonster: Boolean get() = isMonster && (isFusion || isSynchro || isXyz || isLink)

    /** 主卡组怪兽：小世界只能在手牌和卡组之间用。 */
    val isMainMonster: Boolean get() = isMonster && !isExtraMonster

    /** 等级 / 阶级 / 连接值。 */
    val level: Int get() = (rawLevel and 0xFF).toInt()

    /** 攻击力；「?」返回 null。 */
    val atkValue: Int? get() = if (atk < 0) null else atk

    /** 守备力；「?」或连接怪兽返回 null。 */
    val defValue: Int? get() = if (isLink || def < 0) null else def

    val atkUnknown: Boolean get() = atk < 0
    val defUnknown: Boolean get() = !isLink && def < 0

    val raceName: String get() = raceName(race)
    val attributeName: String get() = attributeName(attribute)

    companion object {
        const val TYPE_MONSTER = 0x1L
        const val TYPE_FUSION = 0x40L
        const val TYPE_SYNCHRO = 0x2000L
        const val TYPE_XYZ = 0x800000L
        const val TYPE_LINK = 0x4000000L

        private val ATTRIBUTES = linkedMapOf(
            0x01L to "地", 0x02L to "水", 0x04L to "炎", 0x08L to "风",
            0x10L to "光", 0x20L to "暗", 0x40L to "神",
        )

        private val RACES = linkedMapOf(
            0x1L to "战士族", 0x2L to "魔法师族", 0x4L to "天使族", 0x8L to "恶魔族",
            0x10L to "不死族", 0x20L to "机械族", 0x40L to "水族", 0x80L to "炎族",
            0x100L to "岩石族", 0x200L to "鸟兽族", 0x400L to "植物族", 0x800L to "昆虫族",
            0x1000L to "雷族", 0x2000L to "龙族", 0x4000L to "兽族", 0x8000L to "兽战士族",
            0x10000L to "恐龙族", 0x20000L to "鱼族", 0x40000L to "海龙族", 0x80000L to "爬虫类族",
            0x100000L to "念动力族", 0x200000L to "幻神兽族", 0x400000L to "创造神族",
            0x800000L to "幻龙族", 0x1000000L to "电子界族", 0x2000000L to "幻想魔族",
        )

        fun attributeName(bits: Long): String =
            ATTRIBUTES.filterKeys { bits and it != 0L }.values.joinToString("/").ifEmpty { "?" }

        fun raceName(bits: Long): String =
            RACES.filterKeys { bits and it != 0L }.values.joinToString("/").ifEmpty { "?" }

        /** 解析 ygocdb v0 单卡接口；结构不对返回 null。 */
        fun fromYgocdb(json: JSONObject, fallbackCode: Long): CardInfo? {
            val data = json.optJSONObject("data") ?: return null
            if (!data.has("type")) return null
            val text = json.optJSONObject("text")
            val name = listOf(
                text?.optString("name"),
                json.optString("cn_name"),
                text?.optString("sc_name"),
                text?.optString("jp_name"),
                text?.optString("en_name"),
            ).firstOrNull { !it.isNullOrBlank() } ?: "#$fallbackCode"
            val code = json.optLong("id", fallbackCode).takeIf { it > 0 } ?: fallbackCode
            return CardInfo(
                code = code,
                name = name,
                type = data.optLong("type"),
                atk = data.optInt("atk"),
                def = data.optInt("def"),
                rawLevel = data.optLong("level"),
                race = data.optLong("race"),
                attribute = data.optLong("attribute"),
            )
        }

        /** 本地缓存用的紧凑 JSON。 */
        fun toCacheJson(c: CardInfo): JSONObject = JSONObject()
            .put("code", c.code)
            .put("name", c.name)
            .put("type", c.type)
            .put("atk", c.atk)
            .put("def", c.def)
            .put("level", c.rawLevel)
            .put("race", c.race)
            .put("attribute", c.attribute)

        fun fromCacheJson(o: JSONObject): CardInfo? {
            val code = o.optLong("code", 0L)
            if (code <= 0) return null
            return CardInfo(
                code = code,
                name = o.optString("name", "#$code"),
                type = o.optLong("type"),
                atk = o.optInt("atk"),
                def = o.optInt("def"),
                rawLevel = o.optLong("level"),
                race = o.optLong("race"),
                attribute = o.optLong("attribute"),
            )
        }
    }
}
