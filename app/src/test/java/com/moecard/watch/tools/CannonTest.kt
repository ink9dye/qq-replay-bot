package com.moecard.watch.tools

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CannonTest {

    @Test
    fun `ideal solution is x = A minus B and y = 2B minus A`() {
        val s = Cannon.ideal(14, 10)!!
        assertEquals(4, s.rank)   // 14 - 10
        assertEquals(6, s.level)  // 20 - 14
        assertNull(Cannon.ideal(10, 10)) // 阶级 0 不存在
        assertNull(Cannon.ideal(30, 10)) // 融合等级为负
    }

    @Test
    fun `step one needs two xyz of the same rank and one fusion`() {
        val extra = Cannon.Pool(xyz = mapOf(4 to 2, 2 to 1), fusion = mapOf(6 to 1, 10 to 1))
        val options = Cannon.step1Options(14, extra)
        assertEquals(listOf(Cannon.Step1(4, 6)), options)       // 2×4+6；2 阶只有 1 只不行
    }

    @Test
    fun `clears when the just banished pair matches B`() {
        val extra = Cannon.Pool(xyz = mapOf(4 to 2), fusion = mapOf(6 to 1))
        val r = Cannon.solve(14, 10, extra)
        assertEquals(Cannon.Status.CLEAR, r.status)
        assertTrue(r.plans[0].step2[0].fromStep1)
    }

    @Test
    fun `banish only when the second equation has no pair`() {
        val extra = Cannon.Pool(xyz = mapOf(4 to 2), fusion = mapOf(6 to 1))
        val r = Cannon.solve(14, 9, extra)
        assertEquals(Cannon.Status.BANISH_ONLY, r.status)
    }

    @Test
    fun `previously banished cards can serve step two`() {
        val extra = Cannon.Pool(xyz = mapOf(4 to 2), fusion = mapOf(6 to 1))
        val banished = Cannon.Pool(xyz = mapOf(3 to 1), fusion = mapOf(6 to 1))
        val r = Cannon.solve(14, 9, extra, banished)            // 3 阶 + 6 星 = 9
        assertEquals(Cannon.Status.CLEAR, r.status)
        val s2 = r.plans[0].step2.first()
        assertEquals(3, s2.rank)
        assertEquals(6, s2.level)
        assertTrue(!s2.fromStep1)
    }

    @Test
    fun `none when extra deck cannot make A`() {
        val extra = Cannon.Pool(xyz = mapOf(4 to 2), fusion = mapOf(6 to 1))
        assertEquals(Cannon.Status.NONE, Cannon.solve(13, 9, extra).status)
    }

    @Test
    fun `pool from cards counts xyz by rank and fusion by level`() {
        val infos = mapOf(
            1L to CardInfo(1, "超量4", 0x800021, 0, 0, 4, 0x1, 0x1),
            2L to CardInfo(2, "融合6", 0x41, 0, 0, 6, 0x1, 0x1),
            3L to CardInfo(3, "连接2", 0x4000021, 0, 0, 2, 0x1, 0x1),
        )
        val pool = Cannon.Pool.fromCards(listOf(1, 1, 2, 3), infos)
        assertEquals(2, pool.xyz[4])
        assertEquals(1, pool.fusion[6])
        assertEquals(1, pool.xyz.size + pool.fusion.size - 1)
    }

    @Test
    fun `recommendation covers what it claims within the slot budget`() {
        val aRange = 8..18
        val bRange = 1..12
        val list = Cannon.recommend(6, aRange, bRange)
        assertTrue(list.isNotEmpty())
        for (lo in list) {
            assertTrue(lo.slots <= 6)
            var pool = Cannon.Pool()
            lo.ranks.forEach { pool = pool.withXyz(it, 2) }
            lo.levels.forEach { pool = pool.withFusion(it, 1) }
            var clear = 0
            for (a in aRange) for (b in bRange) {
                if (Cannon.solve(a, b, pool).status == Cannon.Status.CLEAR) clear++
            }
            assertEquals(lo.covered, clear)
        }
        // 结果按覆盖数从高到低
        assertEquals(list.map { it.covered }.sortedDescending(), list.map { it.covered })
    }
}
