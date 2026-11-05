package com.moecard.watch.protocol

import org.tukaani.xz.LZMAInputStream
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream

/**
 * 从 .yrp 里读出**录像自己的**双方名字顺序。
 *
 * 为什么需要：服务端校验小局胜负证据时会逐字比对
 * `evidence.players == replay 里读出来的 names`（见 bot/statistics_import.py 的 valid_live）。
 * 房间列表里的玩家顺序和录像里的顺序不保证一致（实测就有反过来的），
 * 直接拿房间顺序去写证据会被服务端整条丢弃。
 *
 * 解析规则照抄服务端 watch.py 的 `decompress_replay()` + `decks_from_replay()`：
 *  - 头部 80 字节（yrp2）/ 32 字节（yrp1），`flag & 0x1` 表示正文是 LZMA1 raw 流；
 *  - 解压后开头是 N 个 40 字节的 UTF-16LE 昵称，N = 4（flag & 0x2）否则 2。
 */
object ReplayDecode {
    private const val MAGIC_YRP1 = 0x31707279L
    private const val MAGIC_YRP2 = 0x32707279L
    private const val MAX_BODY = 0x80000

    /** 录像里的双方名字，顺序即服务端使用的顺序；解析不出来返回 null。 */
    fun playerNames(blob: ByteArray): List<String>? = try {
        val data = body(blob)
        if (data == null) {
            null
        } else {
            val flag = readU32(blob, 8).toInt()
            val count = if (flag and 0x2 != 0) 4 else 2
            if (data.size < count * 40 + 16) {
                null
            } else {
                val names = (0 until count).map { index ->
                    YgoProtocol.decodeUtf16z(data.copyOfRange(index * 40, index * 40 + 40)).trim()
                }
                names.takeIf { all -> all.all { it.isNotEmpty() } }
            }
        }
    } catch (_: Exception) {
        // 头部/流损坏：交给调用方回退到房间名单，绝不让解析异常打断录制
        null
    }

    /** 解出 .yrp 正文；与服务端一致，解压长度必须等于头部记录的 datasize。 */
    fun body(blob: ByteArray): ByteArray? {
        if (blob.size < 32) return null
        val magic = readU32(blob, 0)
        if (magic != MAGIC_YRP1 && magic != MAGIC_YRP2) return null
        val flag = readU32(blob, 8).toInt()
        val size = readU32(blob, 16).toInt()
        if (size <= 0 || size > MAX_BODY) return null
        val header = if (magic == MAGIC_YRP2) 80 else 32
        if (blob.size < header) return null
        if (flag and 0x1 == 0) {
            if (blob.size < header + size) return null
            return blob.copyOfRange(header, header + size)
        }
        if (blob.size < 29) return null
        val props = blob[24].toInt() and 0xFF
        if (props == 0) return null
        val dictionary = readU32(blob, 25).toInt()
        if (dictionary < 4096) return null
        val stream = LZMAInputStream(
            ByteArrayInputStream(blob, header, blob.size - header),
            // 必须传 -1（长度未知，读流内结束标记）。传 datasize 让 xz-java 走“已知长度”分支，
            // 在萌卡的录像上会直接抛 CorruptedInputException，实测复现见 2026-11-05。
            -1L,
            props.toByte(),
            dictionary,
        )
        stream.use { input ->
            val out = ByteArrayOutputStream(size)
            val buffer = ByteArray(8 * 1024)
            var total = 0
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                total += read
                if (total > size) return null
                out.write(buffer, 0, read)
            }
            return if (total == size) out.toByteArray() else null
        }
    }

    private fun readU32(bytes: ByteArray, at: Int): Long =
        (bytes[at].toLong() and 0xFF) or
            ((bytes[at + 1].toLong() and 0xFF) shl 8) or
            ((bytes[at + 2].toLong() and 0xFF) shl 16) or
            ((bytes[at + 3].toLong() and 0xFF) shl 24)
}
