package com.moecard.watch.tools

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SmallWorldTest {
    // 种族 / 属性位按 YGOPro 口径
    private val DRAGON = 0x2000L
    private val SPELLCASTER = 0x2L
    private val WARRIOR = 0x1L
    private val LIGHT = 0x10L
    private val DARK = 0x20L
    private val EARTH = 0x01L

    private fun m(code: Long, race: Long, attr: Long, level: Int, atk: Int?, def: Int?, copies: Int = 1) =
        SwMonster(code, "#$code", race, attr, level, atk, def, copies)

    @Test
    fun `exactly one shared property links two monsters`() {
        val a = m(1, DRAGON, LIGHT, 8, 3000, 2500)
        val b = m(2, DRAGON, DARK, 4, 1800, 1000)
        assertEquals(SwDim.RACE, SmallWorld.link(a, b))
    }

    @Test
    fun `two shared properties do not link`() {
        val a = m(1, DRAGON, LIGHT, 8, 3000, 2500)
        val b = m(2, DRAGON, LIGHT, 4, 1800, 1000)
        assertNull(SmallWorld.link(a, b))
    }

    @Test
    fun `question mark stats are excluded unless treated as zero`() {
        val a = m(1, DRAGON, LIGHT, 8, null, 0)
        val b = m(2, WARRIOR, DARK, 4, 1800, 0)
        assertNull(SmallWorld.sharedDims(a, b))
        assertEquals(listOf(SwDim.DEF), SmallWorld.sharedDims(a, b, questionAsZero = true))
    }

    @Test
    fun `route needs a bridge from the deck even when hand and target share one property`() {
        val hand = m(1, DRAGON, LIGHT, 8, 3000, 2500)
        val target = m(2, DRAGON, DARK, 4, 1800, 1000) // 与手牌恰好一项相同
        // 卡组里没有别的怪兽：小世界必须先展示卡组里的另一只，所以无路可走
        assertTrue(SmallWorld.routes(hand, target, listOf(hand, target)).isEmpty())
    }

    @Test
    fun `finds bridges and the shared property of each half`() {
        val hand = m(1, DRAGON, LIGHT, 8, 3000, 2500)
        val bridge = m(3, SPELLCASTER, LIGHT, 4, 1500, 1200)  // 与手牌：属性
        val target = m(2, WARRIOR, EARTH, 4, 1800, 1000)      // 与跳板：等级
        val routes = SmallWorld.routes(hand, target, listOf(hand, bridge, target))
        assertEquals(1, routes.size)
        assertEquals(3L, routes[0].bridge.code)
        assertEquals(SwDim.ATTRIBUTE, routes[0].first)
        assertEquals(SwDim.LEVEL, routes[0].second)
    }

    @Test
    fun `single copy in hand cannot also be searched from the deck`() {
        val hand = m(1, DRAGON, LIGHT, 8, 3000, 2500, copies = 1)
        val bridge = m(3, SPELLCASTER, LIGHT, 4, 1500, 1200)
        assertTrue(SmallWorld.routes(hand, hand, listOf(hand, bridge)).isEmpty())
        val twoCopies = hand.copy(copies = 2)
        // 有第二张时可以经跳板检索同名卡：跳板与它只有等级相同
        val bridge8 = m(4, SPELLCASTER, DARK, 8, 1500, 1200)
        val routes = SmallWorld.routes(twoCopies, twoCopies, listOf(twoCopies, bridge8))
        assertEquals(1, routes.size)
        assertEquals(SwDim.LEVEL, routes[0].first)
    }

    @Test
    fun `reachable lists every target with its bridges`() {
        val hand = m(1, DRAGON, LIGHT, 8, 3000, 2500)
        val bridge = m(3, SPELLCASTER, LIGHT, 4, 1500, 1200)
        val t1 = m(2, WARRIOR, EARTH, 4, 1800, 1000)
        val t2 = m(5, SPELLCASTER, DARK, 3, 900, 300)        // 与跳板：种族
        val reach = SmallWorld.reachable(hand, listOf(hand, bridge, t1, t2))
        val codes = reach.map { it.first.code }.toSet()
        assertTrue(2L in codes)
        assertTrue(5L in codes)
    }

    @Test
    fun `deck monsters drop extra deck and non monsters and keep copies`() {
        val infos = mapOf(
            10L to CardInfo(10, "怪兽", 0x21, 1000, 1000, 4, WARRIOR, EARTH),
            11L to CardInfo(11, "超量", 0x800021, 2000, 2000, 4, WARRIOR, EARTH),
            12L to CardInfo(12, "魔法", 0x2, 0, 0, 0, 0, 0),
        )
        val list = SmallWorld.deckMonsters(listOf(10, 10, 11, 12), infos)
        assertEquals(1, list.size)
        assertEquals(2, list[0].copies)
    }
}
