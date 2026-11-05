package com.moecard.watch.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder

class ProtocolTest {

    @Test
    fun `frame encode matches watch_py layout`() {
        val frame = YgoProtocol.buildFrame(0x17, "hello".toByteArray(Charsets.US_ASCII))
        // len(6) = 06 00, proto = 17, then "hello"
        assertEquals("06001768656c6c6f", frame.joinToString("") { "%02x".format(it) })
        assertEquals(8, frame.size)
    }

    @Test
    fun `frame round trip decodes proto and body`() {
        val body = byteArrayOf(1, 2, 3, 4)
        val frame = YgoProtocol.buildFrame(0x12, body)
        val result = YgoProtocol.readFrame(frame.inputStream())!!
        assertEquals(0x12, result.first)
        assertArrayEquals(body, result.second)
    }

    @Test
    fun `utf16Fixed pads to 40 bytes`() {
        val f = YgoProtocol.utf16Fixed("abc", 20)
        assertEquals(40, f.size)
    }

    @Test
    fun `mycard password matches Python reference`() {
        // 直接从 ygowatch/watch.py 计算得到的参考值
        val pwd = YgoProtocol.encodeMycardPassword(12345, 3, "room123")
        assertEquals("6gA6MDowroom123", pwd)
        assertEquals(15, pwd.length)
    }

    @Test
    fun `replay split handles concatenated yrp1 files`() {
        val f1 = yrp1(10, "ABCDEFGHIJ".toByteArray())
        val f2 = yrp1(5, "XYZWV".toByteArray())
        val files = ReplaySplitter.split(f1 + f2)
        assertEquals(2, files.size)
        assertEquals(32 + 10, files[0].size)
        assertEquals(32 + 5, files[1].size)
    }

    @Test
    fun `replay split handles single yrp2 file`() {
        val f = yrp2(10, "1234567890".toByteArray())
        val files = ReplaySplitter.split(f)
        assertEquals(1, files.size)
        assertEquals(80 + 10, files[0].size)
    }

    @Test
    fun `replay split stops at non-magic tail`() {
        val f = yrp1(4, "DATA".toByteArray())
        val garbage = byteArrayOf(0x00, 0x01, 0x02)
        val files = ReplaySplitter.split(f + garbage)
        assertEquals(1, files.size)
        assertEquals(32 + 4, files[0].size)
    }

    // ---- 2026-11-05 小局胜负 / 时间戳修复 ----

    @Test
    fun `replay header reads duel timestamp at offset 20`() {
        val blob = yrp1(10, ByteArray(10))
        ByteBuffer.wrap(blob).order(ByteOrder.LITTLE_ENDIAN).putInt(20, 1793857368)
        assertEquals(1793857368L, ReplayHeader.timestampSeconds(blob))
    }

    @Test
    fun `replay header rejects absurd timestamp`() {
        val blob = yrp1(10, ByteArray(10)) // 偏移 20 全是 0
        assertNull(ReplayHeader.timestampSeconds(blob))
    }

    @Test
    fun `replay decode reads player names in replay order`() {
        val names = ByteArray(2 * 40)
        System.arraycopy("SSER".toByteArray(Charsets.UTF_16LE), 0, names, 0, 8)
        System.arraycopy("ZreaKenny".toByteArray(Charsets.UTF_16LE), 0, names, 40, 18)
        val data = names + ByteArray(16)
        val blob = yrp2(data.size, data)
        assertEquals(listOf("SSER", "ZreaKenny"), ReplayDecode.playerNames(blob))
    }

    @Test
    fun `replay decode returns null when deck section is truncated`() {
        val blob = yrp2(40, ByteArray(40))
        assertNull(ReplayDecode.playerNames(blob))
    }

    @Test
    fun `live evidence attaches capture only to the last replay of a batch`() {
        val evidence = LiveEvidence()
        evidence.onDuelStart()
        evidence.onGameStart(gameStart(0x10))
        evidence.onGameMessage(ByteArray(17) { 1 }.also { it[0] = 50 })
        val capture = evidence.onWin(0, 1, byteArrayOf(5, 0, 1))
        assertTrue(capture.capturedFromStart)
        assertEquals(2, capture.events.size) // 一条 MSG_UPDATE + 一条 MSG_WIN

        val batch = evidence.assign(listOf(ByteArray(3) { 1 }, ByteArray(3) { 2 }, ByteArray(3) { 3 }))
        assertEquals(3, batch.blobs.size)
        assertNull(batch.blobs[0].capture)
        assertNull(batch.blobs[1].capture)
        assertNotNull(batch.blobs[2].capture)
        assertEquals(0, batch.blobs[2].capture!!.winner)
        // association 的口径与官方参考包一致，且不能写 none
        assertTrue(batch.blobs[2].association.startsWith("STOC_DUEL_START/MSG_START"))
    }

    @Test
    fun `live evidence marks captured_from_start only for observer starts`() {
        val evidence = LiveEvidence()
        // 没收到 STOC_DUEL_START 就进房：即使收到 MSG_START 也不算完整采集
        evidence.onGameStart(gameStart(0x10))
        evidence.onGameMessage(ByteArray(2) { 1 }.also { it[0] = 40 })
        val midGame = evidence.onWin(1, 0, byteArrayOf(5, 1, 0))
        assertFalse(midGame.capturedFromStart)

        val evidence2 = LiveEvidence()
        evidence2.onDuelStart()
        evidence2.onGameStart(gameStart(0x01)) // 非观战者起点
        val notObserver = evidence2.onWin(1, 0, byteArrayOf(5, 1, 0))
        assertFalse(notObserver.capturedFromStart)
    }

    @Test
    fun `live evidence ignores messages outside the length whitelist`() {
        val evidence = LiveEvidence()
        evidence.onDuelStart()
        evidence.onGameStart(gameStart(0x10))
        evidence.onGameMessage(byteArrayOf(50, 1, 2, 3))            // 长度不对，丢掉
        evidence.onGameMessage(ByteArray(40) { 1 }.also { it[0] = 9 }) // 不在白名单，丢掉
        evidence.onGameMessage(byteArrayOf(90, 0, 1, 0, 0, 0, 1))   // MSG_DRAW 3+4*1，收下
        val capture = evidence.onWin(0, 0, byteArrayOf(5, 0, 0))
        assertEquals(2, capture.events.size) // 只有 DRAW 和 WIN
    }

    @Test
    fun `live evidence attaches capture to a single replay`() {
        val evidence = LiveEvidence()
        val capture = evidence.onWin(2, 0, byteArrayOf(5, 2, 0)) // 平局
        assertNotNull(capture)
        val batch = evidence.assign(listOf(ByteArray(3) { 9 }))
        assertTrue(batch.blobs[0].association.startsWith("STOC_GAME_MSG/MSG_WIN"))
        assertEquals(2, batch.blobs[0].capture!!.winner)
    }

    @Test
    fun `live evidence drops the capture once it has been assigned`() {
        val evidence = LiveEvidence()
        evidence.onWin(1, 0, byteArrayOf(5, 1, 0))
        evidence.assign(listOf(ByteArray(3) { 1 }))
        assertNull(evidence.assign(listOf(ByteArray(3) { 2 })).blobs[0].capture)
    }

    // ---- v1.0.5：可靠局次与胜负匹配（对齐安卓旁证修复清单 A1/A2） ----

    @Test
    fun `live evidence keeps pending win when the next game starts`() {
        val evidence = LiveEvidence()
        evidence.onDuelStart()
        evidence.onGameStart(gameStart(0x10))                 // 第 1 局
        evidence.onWin(1, 0, byteArrayOf(5, 1, 0))            // 胜负先到，录像还没来
        evidence.onGameStart(gameStart(0x10))                 // 第 2 局已经开了
        val batch = evidence.assign(listOf(ByteArray(3) { 1 })) // 此时补发第 1 局录像
        // 不能因为新局开始就把胜负丢掉；也不能配给还没结束的第 2 局
        assertEquals(1, batch.blobs[0].serial)
        assertEquals(1, batch.blobs[0].capture!!.winner)
        assertEquals("duel_start", batch.blobs[0].gameNoSource)
        assertEquals(1, batch.blobs[0].gameNo)
    }

    @Test
    fun `live evidence does not label game numbers without a reliable basis`() {
        val evidence = LiveEvidence()
        // 中途进房：没有 STOC_DUEL_START，收到 MSG_START 只是内部序号
        evidence.onGameStart(gameStart(0x10))
        evidence.onWin(0, 1, byteArrayOf(5, 0, 1))
        val batch = evidence.assign(listOf(ByteArray(5) { 3 }))
        assertNull(batch.blobs[0].gameNo)
        assertEquals("unknown", batch.blobs[0].gameNoSource)
        assertEquals("unknown", batch.blobs[0].capture!!.gameNoSource)
        assertNull(batch.blobs[0].capture!!.gameNo)
    }

    @Test
    fun `announce gives authoritative game number without dropping pending win`() {
        val evidence = LiveEvidence()
        evidence.onDuelStart()
        evidence.onGameStart(gameStart(0x10))
        evidence.onWin(0, 1, byteArrayOf(5, 0, 1))
        evidence.onAnnounce(2)                                  // 发牌姬：接下来是第 2 局录像
        val batch = evidence.assign(listOf(ByteArray(4) { 2 }))
        assertEquals(2, batch.blobs[0].gameNo)
        assertEquals("announce", batch.blobs[0].gameNoSource)
        assertEquals(0, batch.blobs[0].capture!!.winner)
    }

    @Test
    fun `live evidence never pairs a win with a different game serial`() {
        val evidence = LiveEvidence()
        evidence.onDuelStart()
        evidence.onGameStart(gameStart(0x10))                  // 第 1 局
        evidence.onWin(0, 0, byteArrayOf(5, 0, 0))
        evidence.assign(listOf(ByteArray(3) { 1 }))            // 第 1 局录像配掉了
        evidence.onGameStart(gameStart(0x10))                  // 第 2 局
        // 第 1 局的胜负没有重复下发；第 2 局录像不该拿到任何胜负
        val batch = evidence.assign(listOf(ByteArray(3) { 2 }))
        assertNull(batch.blobs[0].capture)
    }

    @Test
    fun `consume prevents a late win from being paired again`() {
        val evidence = LiveEvidence()
        evidence.onDuelStart()
        evidence.onGameStart(gameStart(0x10))
        val capture = evidence.onWin(1, 0, byteArrayOf(5, 1, 0))
        evidence.consume(capture)                               // 上层已配到已落盘录像
        val batch = evidence.assign(listOf(ByteArray(3) { 1 }))
        assertNull(batch.blobs[0].capture)
    }

    @Test
    fun `backfilled replays keep their own serial when a new game already started`() {
        // 2026-11-05 实测：三份录像在新局已经开始之后才逐帧下发，
        // 旧逻辑把第 2、3 份都算成“最近开始的那局”，导致第 3 局的胜负配到第 2 份录像。
        val evidence = LiveEvidence()
        evidence.onDuelStart()
        evidence.onGameStart(gameStart(0x10))                  // 第 1 局
        evidence.onWin(1, 0, byteArrayOf(5, 1, 0))             // 第 1 局胜负
        evidence.onGameStart(gameStart(0x10))                  // 第 2 局
        evidence.onGameStart(gameStart(0x10))                  // 第 3 局（比录像先到）

        val g1 = evidence.assign(listOf(ByteArray(3) { 1 })).blobs[0]
        val g2 = evidence.assign(listOf(ByteArray(3) { 2 })).blobs[0]
        val g3 = evidence.assign(listOf(ByteArray(3) { 3 })).blobs[0]

        assertEquals(1, g1.serial)
        assertEquals(1, g1.capture!!.winner)
        assertEquals(2, g2.serial)
        assertNull(g2.capture)
        assertEquals(3, g3.serial)                             // 不能再重复算成第 2 局
        assertNull(g3.capture)

        // 第 3 局的胜负随后才到，序号必须仍能对上第 3 局录像
        val late = evidence.onWin(0, 1, byteArrayOf(5, 0, 1))
        assertEquals(3, late.serial)
    }

    /** MSG_START：类型 4 + 玩家类型 + 17 字节，共 19 字节。 */
    private fun gameStart(playerType: Int): ByteArray =
        ByteArray(19).also {
            it[0] = 4
            it[1] = playerType.toByte()
        }

    // ---- helpers ----
    private fun yrp1(datasize: Int, data: ByteArray): ByteArray {
        val header = ByteBuffer.allocate(32).order(ByteOrder.LITTLE_ENDIAN)
        header.put("yrp1".toByteArray())          // magic
        header.putInt(1)                          // version
        header.putInt(0)                          // flag
        header.putInt(0)                          // seed
        header.putInt(datasize)                   // datasize @ offset 16
        // remaining 12 bytes already zero
        return header.array() + data
    }

    private fun yrp2(datasize: Int, data: ByteArray): ByteArray {
        val header = ByteBuffer.allocate(80).order(ByteOrder.LITTLE_ENDIAN)
        header.put("yrp2".toByteArray())          // magic
        header.putInt(1)
        header.putInt(0)
        header.putInt(0)
        header.putInt(datasize)                   // datasize @ offset 16
        return header.array() + data
    }
}
