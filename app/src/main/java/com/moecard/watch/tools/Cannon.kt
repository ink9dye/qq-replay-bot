package com.moecard.watch.tools

/**
 * 联栗炮固定式（連慄砲固定式 / Simultaneous Equation Cannons）计算。
 *
 * 效果分两步（按卡片效果原文与官方 Q&A 整理）：
 *  1. 从自己额外卡组除外 **2 只相同阶级的超量怪兽 + 1 只融合怪兽**，
 *     要求  2 × 超量阶级 + 融合等级 = 双方手牌与场上的卡片总数（A）。
 *  2. 那之后可以：选对方场上 1 只表侧怪兽，让自己**除外状态**的 1 只超量 + 1 只融合回到额外卡组，
 *     要求  超量阶级 + 融合等级 = 那只怪兽的等级 / 阶级（B），然后把对方场上的卡全部除外。
 *     第 2 步用的不必是第 1 步刚除外的那几只，除外区原有的也可以。
 *
 * 只用第 1 步刚除外的卡时，方程组 2x+y=A、x+y=B 的解就是 x = A−B（阶级）、y = 2B−A（融合等级）。
 *
 * 注意：duelkit 把 x、y 当成“某个阶级需要几张卡”，并让用户自己选阶级映射，那是对效果的误读。
 */
object Cannon {
    const val RANK_MIN = 1
    const val RANK_MAX = 13
    const val LEVEL_MIN = 1
    const val LEVEL_MAX = 12

    /** 一组超量 / 融合怪兽的数量统计：阶级 → 张数、等级 → 张数。 */
    data class Pool(
        val xyz: Map<Int, Int> = emptyMap(),
        val fusion: Map<Int, Int> = emptyMap(),
    ) {
        val isEmpty: Boolean get() = xyz.values.sum() == 0 && fusion.values.sum() == 0

        fun plus(rank: Int, xyzCount: Int, level: Int, fusionCount: Int): Pool = Pool(
            xyz = xyz + (rank to ((xyz[rank] ?: 0) + xyzCount)),
            fusion = fusion + (level to ((fusion[level] ?: 0) + fusionCount)),
        )

        fun withXyz(rank: Int, count: Int): Pool =
            copy(xyz = if (count <= 0) xyz - rank else xyz + (rank to count))

        fun withFusion(level: Int, count: Int): Pool =
            copy(fusion = if (count <= 0) fusion - level else fusion + (level to count))

        companion object {
            /** 从额外卡组的卡片数据统计（只看超量与融合；同调 / 连接与本卡无关）。 */
            fun fromCards(extraCodes: List<Long>, infos: Map<Long, CardInfo>): Pool {
                val xyz = HashMap<Int, Int>()
                val fusion = HashMap<Int, Int>()
                for (code in extraCodes) {
                    val c = infos[code] ?: continue
                    if (!c.isMonster || c.isLink) continue
                    when {
                        c.isXyz -> xyz[c.level] = (xyz[c.level] ?: 0) + 1
                        c.isFusion -> fusion[c.level] = (fusion[c.level] ?: 0) + 1
                    }
                }
                return Pool(xyz.toSortedMap(), fusion.toSortedMap())
            }
        }
    }

    /** 第 1 步：除外 2 只 [rank] 阶超量 + 1 只 [level] 星融合。 */
    data class Step1(val rank: Int, val level: Int)

    /** 第 2 步：除外区的 1 只 [rank] 阶超量 + 1 只 [level] 星融合回额外卡组。 */
    data class Step2(
        val rank: Int,
        val level: Int,
        /** 两只都是第 1 步刚除外的（不依赖除外区原有的卡）。 */
        val fromStep1: Boolean,
    )

    data class Plan(val step1: Step1, val step2: List<Step2>)

    enum class Status(val label: String) {
        CLEAR("能清场"),
        BANISH_ONLY("能发动、清不了场"),
        NONE("发动不了"),
    }

    data class Result(
        val a: Int,
        val b: Int,
        val status: Status,
        val plans: List<Plan>,
        /** 只用第 1 步那三张时的理想解（x=A−B、y=2B−A）；超出阶级 / 等级范围为 null。 */
        val ideal: Step1?,
    )

    /** x = A−B、y = 2B−A：只用同一组卡同时满足两步的唯一解。 */
    fun ideal(a: Int, b: Int): Step1? {
        val rank = a - b
        val level = 2 * b - a
        if (rank !in RANK_MIN..RANK_MAX || level !in LEVEL_MIN..LEVEL_MAX) return null
        return Step1(rank, level)
    }

    /** 额外卡组 [extra] 在总数 [a] 下能做的所有第 1 步。 */
    fun step1Options(a: Int, extra: Pool): List<Step1> {
        val out = ArrayList<Step1>()
        for ((rank, n) in extra.xyz) {
            if (n < 2) continue
            val level = a - 2 * rank
            if (level < LEVEL_MIN) continue
            if ((extra.fusion[level] ?: 0) >= 1) out.add(Step1(rank, level))
        }
        return out.sortedWith(compareBy({ it.rank }, { it.level }))
    }

    /** 做完 [step1] 之后，对等级 / 阶级 [b] 的怪兽能用的第 2 步组合。 */
    fun step2Options(b: Int, step1: Step1, banished: Pool): List<Step2> {
        val after = banished.plus(step1.rank, 2, step1.level, 1)
        val out = ArrayList<Step2>()
        for ((rank, n) in after.xyz) {
            if (n < 1) continue
            val level = b - rank
            if ((after.fusion[level] ?: 0) < 1) continue
            out.add(Step2(rank, level, fromStep1 = rank == step1.rank && level == step1.level))
        }
        // 优先列只用刚除外那几张的组合：不依赖除外区原有的卡，最稳
        return out.sortedWith(compareByDescending<Step2> { it.fromStep1 }.thenBy { it.rank })
    }

    fun solve(a: Int, b: Int, extra: Pool, banished: Pool = Pool()): Result {
        val plans = step1Options(a, extra).map { s1 -> Plan(s1, step2Options(b, s1, banished)) }
            .sortedByDescending { it.step2.isNotEmpty() }
        val status = when {
            plans.any { it.step2.isNotEmpty() } -> Status.CLEAR
            plans.isNotEmpty() -> Status.BANISH_ONLY
            else -> Status.NONE
        }
        return Result(a, b, status, plans, ideal(a, b))
    }

    /** 热力图：行是 A，列是 B。 */
    fun heatmap(extra: Pool, banished: Pool, aRange: IntRange, bRange: IntRange): List<List<Status>> =
        aRange.map { a -> bRange.map { b -> solve(a, b, extra, banished).status } }

    /** 一套推荐的“固定式”额外卡组配置。 */
    data class Loadout(
        /** 各放 2 只的超量阶级。 */
        val ranks: List<Int>,
        /** 各放 1 只的融合等级。 */
        val levels: List<Int>,
        /** 在 A×B 范围里能清场的格数。 */
        val covered: Int,
        val total: Int,
    ) {
        val slots: Int get() = ranks.size * 2 + levels.size
    }

    /**
     * 在 [slots] 个额外卡组格子内，挑出能覆盖最多 (A, B) 组合的阶级与等级。
     *
     * 只考虑额外卡组本身（除外区为空）：此时某个 (A, B) 能清场当且仅当
     * 放了 2 只 A−B 阶超量和 1 只 2B−A 星融合。阶级集合最多 2^13 种，
     * 固定阶级集合后各融合等级的收益互不影响，按收益从高到低贪心取就是最优。
     */
    fun recommend(slots: Int, aRange: IntRange, bRange: IntRange, maxResults: Int = 5): List<Loadout> {
        val total = aRange.count() * bRange.count()
        if (slots < 3 || total == 0) return emptyList()
        // g[阶级][等级]：只用这一对卡能清场的 (A, B) 格数
        val g = Array(RANK_MAX + 1) { IntArray(LEVEL_MAX + 1) }
        for (a in aRange) for (b in bRange) {
            val s = ideal(a, b) ?: continue
            g[s.rank][s.level] += 1
        }
        val rankCount = RANK_MAX - RANK_MIN + 1
        val candidates = ArrayList<Loadout>()
        for (mask in 1 until (1 shl rankCount)) {
            val ranks = (0 until rankCount).filter { mask and (1 shl it) != 0 }.map { it + RANK_MIN }
            val left = slots - ranks.size * 2
            if (left < 1) continue
            val gain = IntArray(LEVEL_MAX + 1)
            for (r in ranks) for (l in LEVEL_MIN..LEVEL_MAX) gain[l] += g[r][l]
            val levels = (LEVEL_MIN..LEVEL_MAX).filter { gain[it] > 0 }
                .sortedByDescending { gain[it] }
                .take(left)
            if (levels.isEmpty()) continue
            // 每个阶级都得真的用上，否则就是白占 2 格
            val used = ranks.all { r -> levels.any { l -> g[r][l] > 0 } }
            if (!used) continue
            val covered = levels.sumOf { gain[it] }
            candidates.add(Loadout(ranks, levels.sorted(), covered, total))
        }
        return candidates
            .sortedWith(compareByDescending<Loadout> { it.covered }.thenBy { it.slots }.thenBy { it.ranks.sum() })
            .distinctBy { it.ranks to it.levels }
            .take(maxResults)
    }
}
