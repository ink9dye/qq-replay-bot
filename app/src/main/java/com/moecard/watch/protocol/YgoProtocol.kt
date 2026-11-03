package com.moecard.watch.protocol

import java.io.InputStream
import java.util.Base64

/**
 * YGOPro / 萌卡 网络协议常量与帧编解码。
 * 与原 PC 端 watch.py 逐字节对齐：帧格式 <长度2字节><协议号1字节><body>。
 */
object YgoProtocol {
    // 客户端 -> 服务器
    const val CTOS_PLAYER_INFO = 0x10
    const val CTOS_JOIN_GAME = 0x12
    const val CTOS_LEAVE_GAME = 0x13
    const val CTOS_HS_TOOBSERVER = 0x21

    // 服务器 -> 客户端
    const val STOC_GAME_MSG = 0x01
    const val STOC_ERROR_MSG = 0x02
    const val STOC_JOIN_GAME = 0x12
    const val STOC_TYPE_CHANGE = 0x13
    const val STOC_DUEL_START = 0x15
    const val STOC_DUEL_END = 0x16
    const val STOC_REPLAY = 0x17
    const val STOC_CHAT = 0x19
    const val STOC_HS_PLAYER_ENTER = 0x20
    const val STOC_HS_PLAYER_CHANGE = 0x21
    const val STOC_HS_WATCH_CHANGE = 0x22

    const val NETPLAYER_OBSERVER = 7
    const val DEFAULT_VERSION = 0x1362

    const val REPLAY_MAGIC_YRP1 = 0x31707279
    const val REPLAY_MAGIC_YRP2 = 0x32707279

    /** 构造一帧：长度 = body 长度 + 1（含协议号）。 */
    fun buildFrame(proto: Int, body: ByteArray = ByteArray(0)): ByteArray {
        require(body.size + 1 <= 0xFFFF)
        val len = body.size + 1
        val out = ByteArray(3 + body.size)
        out[0] = (len and 0xFF).toByte()
        out[1] = ((len ushr 8) and 0xFF).toByte()
        out[2] = (proto and 0xFF).toByte()
        if (body.isNotEmpty()) System.arraycopy(body, 0, out, 3, body.size)
        return out
    }

    /** 从流中阻塞读取一帧，连接关闭/超时返回 null。 */
    fun readFrame(stream: InputStream): Pair<Int, ByteArray>? {
        val header = ByteArray(3)
        if (!readExact(stream, header, 3)) return null
        val len = (header[0].toInt() and 0xFF) or ((header[1].toInt() and 0xFF) shl 8)
        val proto = header[2].toInt() and 0xFF
        if (len < 1) return null
        val bodyLen = len - 1
        val body = ByteArray(bodyLen)
        if (bodyLen > 0 && !readExact(stream, body, bodyLen)) return null
        return proto to body
    }

    private fun readExact(stream: InputStream, buf: ByteArray, n: Int): Boolean {
        var off = 0
        while (off < n) {
            val r = stream.read(buf, off, n - off)
            if (r < 0) return false
            off += r
        }
        return true
    }

    /** UTF-16LE 定长填充（萌卡账号名 / 房间口令用）。 */
    fun utf16Fixed(text: String, chars: Int = 20): ByteArray {
        val encoded = text.toByteArray(Charsets.UTF_16LE)
        val out = ByteArray(chars * 2)
        val copy = minOf(encoded.size, out.size)
        System.arraycopy(encoded, 0, out, 0, copy)
        return out
    }

    fun decodeUtf16z(raw: ByteArray): String {
        var b = raw
        if (b.size % 2 == 1) b = b.copyOfRange(0, b.size - 1)
        val s = String(b, Charsets.UTF_16LE)
        val idx = s.indexOf('\u0000')
        return if (idx >= 0) s.substring(0, idx) else s
    }

    fun playerInfoPacket(name: String): ByteArray =
        buildFrame(CTOS_PLAYER_INFO, utf16Fixed(name, 20))

    /** JOIN_GAME 帧：<HHI 版本/0/gameid> + 20 字 UTF-16LE 口令。 */
    fun joinPacket(version: Int, password: String, gameId: Int = 0): ByteArray {
        val body = ByteArray(8 + 40)
        val v = version and 0xFFFF
        body[0] = (v and 0xFF).toByte()
        body[1] = ((v ushr 8) and 0xFF).toByte()
        body[2] = 0
        body[3] = 0
        body[4] = (gameId and 0xFF).toByte()
        body[5] = ((gameId ushr 8) and 0xFF).toByte()
        body[6] = ((gameId ushr 16) and 0xFF).toByte()
        body[7] = ((gameId ushr 24) and 0xFF).toByte()
        val pw = utf16Fixed(password, 20)
        System.arraycopy(pw, 0, body, 8, pw.size)
        return buildFrame(CTOS_JOIN_GAME, body)
    }

    /**
     * 生成萌卡房间口令：6 字节动作块按用户密钥异或后做 base64，再拼房间 id 或标题。
     * 与 watch.py 的 encode_mycard_password 完全一致。
     */
    fun encodeMycardPassword(secretId: Int, action: Int, suffix: String, opt0: Int = 0): String {
        val block = ByteArray(6)
        block[1] = (((action and 0xF) shl 4) or (opt0 and 0xF)).toByte()
        val checksum = block[1].toInt() and 0xFF
        block[0] = ((-checksum) and 0xFF).toByte()
        val key = (secretId % 65535) + 1
        val encrypted = ByteArray(6)
        for (offset in intArrayOf(0, 2, 4)) {
            val value = (block[offset].toInt() and 0xFF) or
                ((block[offset + 1].toInt() and 0xFF) shl 8)
            val xored = value xor key
            encrypted[offset] = (xored and 0xFF).toByte()
            encrypted[offset + 1] = ((xored ushr 8) and 0xFF).toByte()
        }
        val token = Base64.getEncoder().encodeToString(encrypted)
        return token + suffix
    }
}
