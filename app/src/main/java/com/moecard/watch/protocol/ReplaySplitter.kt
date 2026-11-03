package com.moecard.watch.protocol

/**
 * STOC_REPLAY 的包体就是 .yrp 录像文件，偶尔会粘上多份。
 * 与 watch.py 的 replay_files_from_payload 逐字节对齐：按魔数切分，单份上限 8MB。
 */
object ReplaySplitter {
    fun split(payload: ByteArray): List<ByteArray> {
        val files = mutableListOf<ByteArray>()
        var buf = payload
        while (buf.size >= 32) {
            val magic = readU32(buf, 0)
            if (magic != YgoProtocol.REPLAY_MAGIC_YRP1 && magic != YgoProtocol.REPLAY_MAGIC_YRP2) break
            val datasize = readU32(buf, 16)
            if (datasize > 8_000_000) break
            val candidates = mutableListOf<Int>()
            if (magic == YgoProtocol.REPLAY_MAGIC_YRP2) candidates += (80 + datasize)
            candidates += (32 + datasize)
            var taken: Int? = null
            for (size in candidates) {
                if (size <= buf.size && size >= 32) {
                    taken = size
                    break
                }
            }
            if (taken == null) {
                files += buf
                break
            }
            files += buf.copyOf(taken)
            buf = if (taken < buf.size) buf.copyOfRange(taken, buf.size) else ByteArray(0)
        }
        return files
    }

    private fun readU32(b: ByteArray, off: Int): Int =
        (b[off].toInt() and 0xFF) or
            ((b[off + 1].toInt() and 0xFF) shl 8) or
            ((b[off + 2].toInt() and 0xFF) shl 16) or
            ((b[off + 3].toInt() and 0xFF) shl 24)
}
