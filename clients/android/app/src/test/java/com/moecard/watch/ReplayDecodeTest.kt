package com.moecard.watch

import com.moecard.watch.protocol.ReplayDecode
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test
import java.security.MessageDigest

/**
 * 用官方参考包 `android-ingest-reference` 的原始录像校验解码结果。
 *
 * 服务端入库时会逐字比对 `evidence.players` 与它自己从 .yrp 读出的名字，
 * 这里对不上，整份旁证就会被丢弃（表现为“局次未知 / 胜负未知 / 场次未可靠关联”）。
 */
class ReplayDecodeTest {

    private fun referenceBytes(): ByteArray =
        javaClass.classLoader!!.getResourceAsStream("reference.yrp")!!.use { it.readBytes() }

    @Test
    fun `reference replay digest matches verification json`() {
        val sha = MessageDigest.getInstance("SHA-256").digest(referenceBytes())
        assertEquals(
            "2b6a4aad599bb8ab08228136b6ed799e5511b101c091dfe69c12683921e5c3cb",
            sha.joinToString("") { "%02x".format(it) },
        )
    }

    @Test
    fun `player names match database order`() {
        val names = ReplayDecode.playerNames(referenceBytes())
        assertNotNull(names)
        // 录像里的顺序，不是房间列表顺序；必须一字不差（含空格/符号）
        assertEquals(listOf("john455668", "娱乐玩家@杏花鸟窝"), names)
        assertEquals(2, names!!.size)
    }
}
