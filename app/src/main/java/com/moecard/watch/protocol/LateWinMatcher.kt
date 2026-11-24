package com.moecard.watch.protocol

/** 一份已落盘、可能需要补写胜负的录像（只取匹配要用的字段，便于单测）。 */
data class LateWinCandidate(
    /** 本任务内的落盘顺序。 */
    val seq: Int,
    /** 本端局序号；未知为 null。 */
    val serial: Int?,
    /** 是否已经带了胜负。 */
    val hasCapture: Boolean,
    /** 落盘时本任务累计收到的 MSG_START 数。 */
    val startsMark: Int,
    /** 落盘时本小局已收到的对局消息数。 */
    val msgsMark: Int,
)

/**
 * “录像先到、胜负后到”时，决定这条胜负该补给哪份录像。
 *
 * 规则（宁可留空也不配错 —— 错胜负比没有胜负更糟）：
 *  1. 只看本任务**最新落盘**的那份录像；它已经有胜负就放弃（胜负留在账本里等下一份录像）。
 *     更早的录像就算缺胜负也不碰：它们的胜负早就错过了，现在到的只可能是更新那局的。
 *  2. 落盘之后不能已经开始了新的小局（[LateWinCandidate.startsMark] 必须等于胜负到达时的计数），
 *     否则这条胜负属于新的那一局。
 *  3. 两边都有本端局序号时必须相同。
 *  4. 序号不全（中途进房）时，要求那份录像是在本小局有对局消息之后才落盘的
 *     （[LateWinCandidate.msgsMark] > 0），排除进房时服务器补发的旧录像。
 *
 * @return 应补写的录像的 [LateWinCandidate.seq]；不该补返回 null。
 */
object LateWinMatcher {
    fun pick(candidates: List<LateWinCandidate>, capture: GameCapture): Int? {
        val latest = candidates.maxByOrNull { it.seq } ?: return null
        if (latest.hasCapture) return null
        if (latest.startsMark != capture.startsTotal) return null
        val gameSerial = latest.serial
        val winSerial = capture.serial
        if (gameSerial != null && winSerial != null) {
            return if (gameSerial == winSerial) latest.seq else null
        }
        return if (latest.msgsMark > 0) latest.seq else null
    }
}
