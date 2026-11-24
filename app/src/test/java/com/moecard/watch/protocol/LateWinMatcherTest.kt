package com.moecard.watch.protocol

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** 2.2.0：晚到的胜负只配给刚落盘、正在等胜负的最后一局。 */
class LateWinMatcherTest {

    private fun win(serial: Int?, starts: Int) = GameCapture(
        winner = 0, reason = 0, raw = "050000", receivedAtMs = 0L,
        serial = serial, gameNo = null, gameNoSource = "unknown",
        events = emptyList(), capturedFromStart = false, startsTotal = starts,
    )

    private fun game(seq: Int, serial: Int?, starts: Int, msgs: Int = 5, has: Boolean = false) =
        LateWinCandidate(seq = seq, serial = serial, hasCapture = has, startsMark = starts, msgsMark = msgs)

    @Test
    fun `late win attaches to the latest game of the same serial`() {
        val games = listOf(game(1, 1, 1, has = true), game(2, 2, 2))
        assertEquals(2, LateWinMatcher.pick(games, win(serial = 2, starts = 2)))
    }

    @Test
    fun `win of game 3 is never written into game 2 that missed its own win`() {
        // 第 2 局录像落盘但没等到胜负；第 3 局的胜负先于第 3 局录像到达
        val games = listOf(game(1, 1, 1, has = true), game(2, 2, 2))
        assertNull(LateWinMatcher.pick(games, win(serial = 3, starts = 3)))
    }

    @Test
    fun `older games without a win are never considered`() {
        // 最新那局已经有胜负：这条胜负属于还没落盘的下一局，不能往前配
        val games = listOf(game(1, 1, 1), game(2, 2, 2, has = true))
        assertNull(LateWinMatcher.pick(games, win(serial = 2, starts = 2)))
    }

    @Test
    fun `a new game started after the replay was saved blocks the match`() {
        val games = listOf(game(1, null, 0))
        assertNull(LateWinMatcher.pick(games, win(serial = null, starts = 1)))
    }

    @Test
    fun `mid room backfill replay without game messages is not matched`() {
        // 中途进房：服务器先补发了旧局录像（本小局还没有任何对局消息），随后才到当前局的胜负
        val games = listOf(game(1, null, 0, msgs = 0))
        assertNull(LateWinMatcher.pick(games, win(serial = null, starts = 0)))
    }

    @Test
    fun `mid room current game replay before its late win is matched`() {
        val games = listOf(game(1, null, 0, msgs = 0), game(2, null, 0, msgs = 120))
        assertEquals(2, LateWinMatcher.pick(games, win(serial = null, starts = 0)))
    }

    @Test
    fun `no saved games means nothing to attach`() {
        assertNull(LateWinMatcher.pick(emptyList(), win(serial = 1, starts = 1)))
    }

    @Test
    fun `live evidence forgets the sticky announce when the next game starts`() {
        val evidence = LiveEvidence()
        evidence.onDuelStart()
        evidence.onGameStart(ByteArray(19).also { it[0] = 4; it[1] = 0x10 })
        evidence.onAnnounce(2)
        evidence.assign(listOf(ByteArray(3) { 2 }))                         // 第 2 局录像，消费掉公告
        evidence.onGameStart(ByteArray(19).also { it[0] = 4; it[1] = 0x10 }) // 第 3 局开始
        val late = evidence.onWin(0, 1, byteArrayOf(5, 0, 1))
        assertNull(late.gameNo)                                               // 不能再沿用「第 2 局」
        assertEquals(2, late.startsTotal)
    }

    @Test
    fun `progress mark counts starts across duels and messages within a game`() {
        val evidence = LiveEvidence()
        evidence.onDuelStart()
        evidence.onGameStart(ByteArray(19).also { it[0] = 4; it[1] = 0x10 })
        evidence.onGameMessage(byteArrayOf(40, 1))
        evidence.onGameMessage(byteArrayOf(9, 9, 9)) // 不在白名单也算进度
        assertEquals(1 to 2, evidence.progressMark())
        evidence.onDuelEnd()
        evidence.onDuelStart()
        evidence.onGameStart(ByteArray(19).also { it[0] = 4; it[1] = 0x10 })
        assertEquals(2 to 0, evidence.progressMark())
    }
}
