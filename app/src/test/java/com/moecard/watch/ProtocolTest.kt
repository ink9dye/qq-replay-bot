package com.moecard.watch.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
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
