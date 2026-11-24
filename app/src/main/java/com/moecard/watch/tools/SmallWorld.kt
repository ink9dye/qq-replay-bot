package com.moecard.watch.tools

/**
 * 小世界（スモール・ワールド）路线计算。
 *
 * 效果：展示手牌 1 只怪兽 H，从卡组选 1 只与 H 在「种族 / 属性 / 等级 / 攻击力 / 守备力」中
 * **恰好 1 项相同**的怪兽 M 展示；再从卡组把 1 只与 M **恰好 1 项相同**的怪兽 T 加入手牌。
 *
 * 与 duelkit 的差异：
 *  - 没有“无需跳板、直接连通”这一说——M 必须是卡组里的另一张卡；
 *  - 只用主卡组怪兽（额外卡组怪兽不会在手牌和卡组里）；
 *  - 攻击力 / 守备力是「?」时默认不参与计算（无法判定就不给结论），可在设置里改成按 0 处理。
 */
enum class SwDim(val label: String) {
    RACE("种族"),
    ATTRIBUTE("属性"),
    LEVEL("等级"),
    ATK("攻击力"),
    DEF("守备力"),
}

/** 参与小世界计算的一只怪兽；atk/def 为 null 表示「?」。 */
data class SwMonster(
    val code: Long,
    val name: String,
    val race: Long,
    val attribute: Long,
    val level: Int,
    val atk: Int?,
    val def: Int?,
    /** 主卡组里的张数。 */
    val copies: Int = 1,
) {
    fun valueText(dim: SwDim): String = when (dim) {
        SwDim.RACE -> CardInfo.raceName(race)
        SwDim.ATTRIBUTE -> CardInfo.attributeName(attribute)
        SwDim.LEVEL -> "$level 星"
        SwDim.ATK -> atk?.toString() ?: "?"
        SwDim.DEF -> def?.toString() ?: "?"
    }

    companion object {
        fun from(info: CardInfo, copies: Int): SwMonster = SwMonster(
            code = info.code,
            name = info.name,
            race = info.race,
            attribute = info.attribute,
            level = info.level,
            atk = info.atkValue,
            def = info.defValue,
            copies = copies,
        )
    }
}

/** 一条路线：手牌 [hand] → 展示卡组里的 [bridge] → 检索 [target]。 */
data class SwRoute(
    val hand: SwMonster,
    val bridge: SwMonster,
    val target: SwMonster,
    /** 手牌与跳板共同的那一项。 */
    val first: SwDim,
    /** 跳板与目标共同的那一项。 */
    val second: SwDim,
)

object SmallWorld {

    /**
     * 两只怪兽相同的项目；有任一项无法判定（「?」且不按 0 处理）时返回 null。
     */
    fun sharedDims(a: SwMonster, b: SwMonster, questionAsZero: Boolean = false): List<SwDim>? {
        val shared = ArrayList<SwDim>(5)
        if (a.race == b.race) shared.add(SwDim.RACE)
        if (a.attribute == b.attribute) shared.add(SwDim.ATTRIBUTE)
        if (a.level == b.level) shared.add(SwDim.LEVEL)
        val atkA = a.atk ?: if (questionAsZero) 0 else return null
        val atkB = b.atk ?: if (questionAsZero) 0 else return null
        if (atkA == atkB) shared.add(SwDim.ATK)
        val defA = a.def ?: if (questionAsZero) 0 else return null
        val defB = b.def ?: if (questionAsZero) 0 else return null
        if (defA == defB) shared.add(SwDim.DEF)
        return shared
    }

    /** 两只怪兽能否作为小世界的一步（恰好一项相同）；返回那一项，不能则 null。 */
    fun link(a: SwMonster, b: SwMonster, questionAsZero: Boolean = false): SwDim? =
        sharedDims(a, b, questionAsZero)?.singleOrNull()

    /**
     * 手牌 [hand] 能否经由卡组里的某只跳板检索到 [target]，列出所有跳板。
     *
     * [deck] 是主卡组怪兽（每种一条，带张数）。手牌那张如果只有 1 张，就不能再从卡组里拿它。
     */
    fun routes(
        hand: SwMonster,
        target: SwMonster,
        deck: List<SwMonster>,
        questionAsZero: Boolean = false,
    ): List<SwRoute> {
        if (!inDeckAfterDraw(target, hand)) return emptyList()
        val out = ArrayList<SwRoute>()
        for (bridge in deck) {
            if (!inDeckAfterDraw(bridge, hand)) continue
            if (bridge.code == target.code) continue
            val first = link(hand, bridge, questionAsZero) ?: continue
            val second = link(bridge, target, questionAsZero) ?: continue
            out.add(SwRoute(hand, bridge, target, first, second))
        }
        return out
    }

    /**
     * 反查：手牌 [hand] 能检索到卡组里的哪些怪兽，每只目标附上全部跳板。
     * 结果按跳板数量从多到少排（越多说明越稳）。
     */
    fun reachable(
        hand: SwMonster,
        deck: List<SwMonster>,
        questionAsZero: Boolean = false,
    ): List<Pair<SwMonster, List<SwRoute>>> {
        val out = ArrayList<Pair<SwMonster, List<SwRoute>>>()
        for (target in deck) {
            val r = routes(hand, target, deck, questionAsZero)
            if (r.isNotEmpty()) out.add(target to r)
        }
        return out.sortedWith(compareByDescending<Pair<SwMonster, List<SwRoute>>> { it.second.size }.thenBy { it.first.name })
    }

    /** 拿掉手牌这一张后，[card] 是否还在卡组里。 */
    private fun inDeckAfterDraw(card: SwMonster, hand: SwMonster): Boolean =
        card.code != hand.code || card.copies >= 2

    /** 卡组卡号（可重复）+ 卡片数据 → 主卡组怪兽列表（每种一条，带张数）。 */
    fun deckMonsters(mainCodes: List<Long>, infos: Map<Long, CardInfo>): List<SwMonster> {
        val counts = LinkedHashMap<Long, Int>()
        for (c in mainCodes) counts[c] = (counts[c] ?: 0) + 1
        return counts.mapNotNull { (code, n) ->
            val info = infos[code] ?: return@mapNotNull null
            if (!info.isMainMonster) null else SwMonster.from(info, n)
        }
    }
}
