package com.moecard.watch.protocol

/**
 * .yrp 头部解析。服务端 `bot/replay_analysis.py` 读的是同一套偏移，必须保持一致。
 *
 * 布局（小端）：
 * ```
 *  0..3   magic   "yrp1" / "yrp2"
 *  4..7   version 对局版本（0x1362 等）
 *  8..11  flag
 * 12..15  seed
 * 16..19  dataSize
 * 20..23  该小局的时间戳（epoch 秒）
 * ```
 *
 * 第 20 字节起那 4 字节是**这一小局**的时间，不是收到录像的时间。用它命名和上报，
 * 服务端才能把三局按真实开局时刻分开，也才可能和萌卡公开历史对上。
 */
object ReplayHeader {
    /** 时间戳合法区间（2017-07 ~ 2039-09），超出就当没有，避免把垃圾头部当成 1970 年。 */
    private const val MIN_SANE = 1_500_000_000L
    private const val MAX_SANE = 2_200_000_000L

    data class Meta(
        val magic: String,
        val version: Int,
        val timestampSeconds: Long?,
    ) {
        val isReplay: Boolean get() = magic == "yrp1" || magic == "yrp2"
    }

    fun parse(blob: ByteArray): Meta {
        val magic = if (blob.size >= 4) String(blob, 0, 4, Charsets.US_ASCII) else ""
        val version = if (blob.size >= 8) readU32(blob, 4).toInt() else 0
        val stamp = if (blob.size >= 24) readU32(blob, 20) else 0L
        return Meta(magic, version, if (stamp in MIN_SANE..MAX_SANE) stamp else null)
    }

    /** 该小局的时间戳（秒）；头部不可用时返回 null。 */
    fun timestampSeconds(blob: ByteArray): Long? = parse(blob).timestampSeconds

    private fun readU32(b: ByteArray, at: Int): Long =
        (b[at].toLong() and 0xFF) or
            ((b[at + 1].toLong() and 0xFF) shl 8) or
            ((b[at + 2].toLong() and 0xFF) shl 16) or
            ((b[at + 3].toLong() and 0xFF) shl 24)
}
