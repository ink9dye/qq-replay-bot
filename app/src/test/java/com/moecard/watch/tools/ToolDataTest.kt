package com.moecard.watch.tools

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ToolDataTest {

    @Test
    fun `parses ygocdb v0 card json`() {
        val raw = """{"id":89631139,"cid":4007,"data":{"ot":11,"setcode":221,"type":17,"atk":3000,""" +
            """"def":2500,"level":8,"race":8192,"attribute":16},"text":{"name":"青眼白龙"}}"""
        val c = CardInfo.fromYgocdb(JSONObject(raw), 89631139)!!
        assertEquals("青眼白龙", c.name)
        assertTrue(c.isMainMonster)
        assertEquals(8, c.level)
        assertEquals("龙族", c.raceName)
        assertEquals("光", c.attributeName)
        assertEquals(3000, c.atkValue)
    }

    @Test
    fun `question mark atk and pendulum level bits`() {
        val c = CardInfo(1, "灵摆?", 0x1000021, -2, 0, (8L shl 24) or (8L shl 16) or 4L, 0x1, 0x10)
        assertNull(c.atkValue)
        assertEquals(4, c.level)
    }

    @Test
    fun `cache json round trip`() {
        val c = CardInfo(5, "x", 0x41, 100, 200, 6, 0x2, 0x20)
        assertEquals(c, CardInfo.fromCacheJson(CardInfo.toCacheJson(c)))
    }

    @Test
    fun `ydk parsing keeps sections and ignores side and comments`() {
        val text = "#created by test\n#main\n111\n111\n222\n#extra\n333\n!side\n444\n"
        val d = ToolDeck.parseYdk(text)!!
        assertEquals(listOf(111L, 111L, 222L), d.main)
        assertEquals(listOf(333L), d.extra)
    }

    @Test
    fun `bare code list counts as main deck`() {
        val d = ToolDeck.parseYdk("89631139\n 46986414 \n")
        assertNotNull(d)
        assertEquals(2, d!!.main.size)
        assertNull(ToolDeck.parseYdk("hello"))
    }
}
