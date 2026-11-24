package com.moecard.watch.ui.query

import com.moecard.watch.query.model.CardWinrate
import com.moecard.watch.query.model.FlowBucket
import com.moecard.watch.query.model.FlowDeck
import com.moecard.watch.query.model.RateBase
import com.moecard.watch.query.model.SideCard
import com.moecard.watch.query.model.SideOpponent
import com.moecard.watch.query.model.SideSeat
import com.moecard.watch.query.model.UsageRecord
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** 2.2.0 换备页 / 单卡统计的数据整理（纯函数部分）。 */
class StatsLayoutTest {

    private fun card(code: Long, games: Int = 2) = SideCard(code, "#$code", "main", 2, games, 1, 1)

    private fun opponent(deck: String, added: List<SideCard>, removed: List<SideCard> = emptyList()) = SideOpponent(
        deck = deck, matches = 2, recentMatches = 2, sideGames = 4, seatUnknownGames = 0, sampleLow = false,
        first = SideSeat(2, added, removed), second = SideSeat(2, emptyList(), emptyList()), sessions = emptyList(),
    )

    @Test
    fun `merge puts a card changed in both seats on one row first`() {
        val rows = mergeSideRows(listOf(card(1), card(2)), listOf(card(2), card(3)))
        assertEquals(3, rows.size)
        assertEquals(2L, rows[0].code)
        assertTrue(rows[0].first != null && rows[0].second != null)
    }

    @Test
    fun `common side cards need a majority of opponents`() {
        val ops = listOf(
            opponent("A", listOf(card(1), card(2))),
            opponent("B", listOf(card(1))),
            opponent("C", listOf(card(1), card(3))),
        )
        val common = commonSideCards(ops, added = true)
        assertEquals(listOf(1L), common.map { it.code })
        assertEquals(3, common[0].decks)
        assertTrue(commonSideCards(ops.take(1), added = true).isEmpty())
    }

    @Test
    fun `carriers drop players without the card and sort by copies`() {
        val list = carriers(
            listOf(
                UsageRecord("a", 1, 0, 0, 0, true, true, true),
                UsageRecord("b", 2, 0, 1, 1, true, true, true),
                UsageRecord("c", 3, 0, 2, 2, true, true, true),
            ),
        )
        assertEquals(listOf("c", "b"), list.map { it.player })
    }

    @Test
    fun `carrying decks hide zero rows and sort by users`() {
        fun b(used: Int, known: Int) = FlowBucket(known, used, used, 0, 0, if (known > 0) used.toDouble() / known else null)
        val decks = carryingDecks(
            listOf(
                FlowDeck("三幻魔", 10, b(0, 10), b(0, 10), b(0, 10)),
                FlowDeck("盈彩月夜", 3, b(0, 3), b(2, 3), b(2, 3)),
                FlowDeck("鹰身", 9, b(0, 9), b(1, 9), b(1, 9)),
            ),
        )
        assertEquals(listOf("盈彩月夜", "鹰身"), decks.map { it.deck })
    }

    @Test
    fun `usage mode summarises where the card is played`() {
        assertEquals("前百没人带", usageMode(Carry(100, 0, 0, 0)))
        assertEquals("全部从 Side 换入", usageMode(Carry(100, 7, 0, 7)))
        assertTrue(usageMode(Carry(100, 10, 3, 8)).startsWith("多数从 Side 换入"))
    }

    @Test
    fun `seat insight only for clear gaps with enough games`() {
        fun w(f: RateBase?, s: RateBase?) =
            CardWinrate(1, "x", null, emptyList(), 0, 0, emptyList(), f, s, null, 0.0)
        val first = RateBase(44, 30, 14, 0, 68.2)
        val second = RateBase(96, 42, 54, 0, 43.8)
        assertEquals("先攻胜率比后攻高 24.4 个百分点", seatInsight(w(first, second)))
        assertNull(seatInsight(w(first, RateBase(5, 1, 4, 0, 20.0))))
        assertNull(seatInsight(w(first, RateBase(50, 32, 18, 0, 64.0))))
    }
}
