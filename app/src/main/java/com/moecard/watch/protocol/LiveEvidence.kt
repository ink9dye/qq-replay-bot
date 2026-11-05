package com.moecard.watch.protocol

/**
 * 一小局的观战证据。
 *
 * `serial` 是本端在本次任务内的局序号（收到几次 `MSG_START`），**只是内部暂存编号**；
 * `gameNo` 才是有可靠依据的真实局次，没有依据时为 null（服务端看到缺省就保留未知，
 * 不能把内部编号冒充真实第 N 局）。
 */
data class GameCapture(
    val winner: Int,
    val reason: Int,
    val raw: String,
    val receivedAtMs: Long,
    val serial: Int?,
    val gameNo: Int?,
    /** `announce`（发牌姬「第N局录像」）/ `duel_start`（从决斗开始连续计数）/ `unknown`。 */
    val gameNoSource: String,
    /** 当局逐条消息的原始 hex（含最后的 MSG_WIN），长度已按服务端白名单校验过。 */
    val events: List<String>,
    /** 只有收到 observer 的 MSG_START 后才是 true；中途进房拿不到完整起始状态。 */
    val capturedFromStart: Boolean,
) {
    /** 给这份胜负补上该录像可确认的局次；没有依据就明确留空。 */
    fun numbered(no: Int?, source: String): GameCapture =
        if (no == null || no <= 0) copy(gameNo = null, gameNoSource = "unknown")
        else copy(gameNo = no, gameNoSource = source)
}

/** 一份待落盘的录像，带上它对应的小局号与（可能还没有的）证据。 */
data class ReplayBlob(
    val bytes: ByteArray,
    /** 本端局序号；未知为 null。 */
    val serial: Int?,
    /** 有可靠依据的真实局次；未知为 null。 */
    val gameNo: Int?,
    val gameNoSource: String,
    val capture: GameCapture?,
    /** 关联依据，写进证据 JSON 便于服务端事后核对。 */
    val association: String,
)

/** 一条 STOC_REPLAY 里可能粘了多局，整批一起交给上层才能判断“哪一份是刚打完的”。 */
data class ReplayBatch(val blobs: List<ReplayBlob>)

/**
 * 观战流的即时证据账本。
 *
 * 与旧实现的关键差别（对齐《安卓与服务器录像旁证修复清单》A1/A2）：
 *  1. 收到的 `MSG_WIN` 放进**待配池**，`MSG_START`（下一局开始）或局次公告到达时**不清空**；
 *     只有真的配到某份录像（或上层明确告知已配到已落盘录像）才消费掉。
 *  2. 按**同一决斗内的局序号**严格匹配胜负与录像，匹配不上就留空，不再“取最近一个尾局”。
 *  3. 局次（`gameNo`）只认两种依据：发牌姬「第N局录像」公告，或从 `STOC_DUEL_START` 起
 *     连续计数（说明本端从这场决斗开局就在场）；其余一律 `unknown`，不用默认 1 或本地递增。
 *  4. 证据账本在同一任务内跨重连共享，断线不丢已收到的胜负。
 */
class LiveEvidence {
    /** 本决斗内的 MSG_START 计数（内部序号，不代表真实局次）。 */
    private var serial = 0
    private var duelActive = false
    /** 收到过本决斗的 STOC_DUEL_START：从这一刻起数是可靠的局次依据。 */
    private var numberingFromDuelStart = false
    /** 发牌姬公告：接下来下发的第一份录像是第 N 局。 */
    private var announced: Int? = null

    /** 尚未配到录像的胜负：键是本端局序号（0 表示没收到 MSG_START，序号未知）。 */
    private val pendingWins = LinkedHashMap<Int, GameCapture>()

    /** 已经配过录像的最大局序号：判断“这份录像是不是已经配过了”。 */
    private var lastAssignedSerial = 0

    /** 当前小局已收到的消息（hex）。 */
    private val current = ArrayList<String>()
    private var currentFromStart = false
    private var anchor = false
    private var overflow = false

    /** 收到 STOC_DUEL_START：说明本端在这场决斗开始时就在场，局次可以从 1 数起。 */
    @Synchronized
    fun onDuelStart() {
        anchor = true
        if (!duelActive) {
            // 新的一场决斗：局次重新从 1 开始，上一场没配上的胜负不能带过来串局。
            duelActive = true
            serial = 0
            lastAssignedSerial = 0
            numberingFromDuelStart = true
            announced = null
            pendingWins.clear()
        }
    }

    /** 收到 STOC_DUEL_END：本场决斗结束，之后再来 STOC_DUEL_START 才算新一场。 */
    @Synchronized
    fun onDuelEnd() {
        duelActive = false
        numberingFromDuelStart = false
    }

    /** 收到 MSG_START（类型 4，正文 19 字节）：新的一小局开始。 */
    @Synchronized
    fun onGameStart(body: ByteArray) {
        serial += 1
        val playerType = if (body.size > 1) body[1].toInt() and 0xFF else 0
        val observerStart = playerType == 0x10 || playerType == 0x11
        currentFromStart = anchor && observerStart
        current.clear()
        overflow = false
        // 注意：这里不能清 pendingWins —— 上一局的 MSG_WIN 可能还没等到它的录像。
    }

    /**
     * 把一条 STOC_GAME_MSG 记进当前小局的消息流。
     *
     * 只收服务端认的长度（{5:3, 40:2, 50:17, 60:9, 61:1, 62:9, 63:1, 64:9, 65:1, 70:17}
     * 加上 MSG_DRAW 的 3+4n 变长），并且最多 10 万条；超了就放弃完整采集标记，
     * 免得把半截消息流当完整证据提交。
     */
    @Synchronized
    fun onGameMessage(body: ByteArray) {
        if (!accepted(body)) return
        if (current.size >= MAX_EVENTS) {
            overflow = true
            return
        }
        current.add(body.toHexString())
    }

    /**
     * 收到 MSG_WIN（正文恰 3 字节：类型/胜方/原因），封成快照放进待配池。
     * 此时还不知道这份胜负对应哪份录像，所以 `gameNo` 留空，由 [assign] 按局序补上。
     */
    @Synchronized
    fun onWin(winner: Int, reason: Int, body: ByteArray): GameCapture {
        if (accepted(body)) onGameMessage(body)
        val capture = GameCapture(
            winner = winner,
            reason = reason,
            raw = body.toHexString(),
            receivedAtMs = System.currentTimeMillis(),
            serial = if (serial > 0) serial else null,
            gameNo = null,
            gameNoSource = "unknown",
            events = current.toList(),
            capturedFromStart = currentFromStart && !overflow,
        )
        pendingWins[if (serial > 0) serial else 0] = capture
        while (pendingWins.size > MAX_PENDING) {
            val oldest = pendingWins.keys.first()
            pendingWins.remove(oldest)
        }
        return capture
    }

    /**
     * 发牌姬的「第N局录像」公告：接下来下发的第一份录像就是第 N 局。
     *
     * 这里**不能**清掉待配胜负：公告常在 MSG_WIN 之后、录像之前到达。
     */
    @Synchronized
    fun onAnnounce(gameNo: Int) {
        if (gameNo in 1..MAX_GAME_NO) announced = gameNo
    }

    /**
     * 上层已经把这份胜负配到某份**已落盘**的录像（录像先到、胜负后到），
     * 告诉账本把它从待配池里消费掉，避免它又被配给下一份录像。
     */
    @Synchronized
    fun consume(capture: GameCapture) {
        pendingWins.remove(if (capture.serial != null) capture.serial else 0)
    }

    /**
     * 给一帧 STOC_REPLAY 拆出来的若干份录像分配局次、序号与证据。
     *
     * 关联规则（宁可留空也不猜错，错胜负比没有胜负更糟）：
     *  - 刚下发的那批录像对应「已收到胜负、还没配过录像」的最新一局；
     *    没有待配胜负时才退回“最近开始、还没配过录像”的那一局；
     *  - 一帧多份时，前面的都是服务器补发的旧局，只有最后一份是刚打完的；
     *  - 只有序号对得上的胜负才配给对应录像，对不上就留空；
     *  - 序号未知（中途进房没收到 MSG_START）且整批只有一份时，只在恰好一条待配胜负时才配。
     */
    @Synchronized
    fun assign(blobs: List<ByteArray>): ReplayBatch {
        if (blobs.isEmpty()) return ReplayBatch(emptyList())
        val n = blobs.size
        val announcedNo = announced
        val pendingMax = pendingWins.keys.filter { it > lastAssignedSerial }.maxOrNull()
        // 下一份录像对应“下一局还没配过录像的小局”，而不是“最近一次 MSG_START”。
        // 反例（2026-11-05 实测）：三份录像在新一局已经开始之后才逐帧下发，
        // 旧写法把每份都当成“最近开始的那局”，结果第 3 份的胜负被配到了第 2 份录像上。
        val nextUnassigned = lastAssignedSerial + 1
        val tailSerial: Int? = when {
            pendingMax != null -> pendingMax
            serial >= nextUnassigned -> nextUnassigned
            else -> null
        }
        val items = blobs.mapIndexed { index, bytes ->
            val offset = n - 1 - index
            val blobSerial = tailSerial?.let { it - offset }?.takeIf { it > 0 }
            val (gameNo, gameNoSource) = when {
                announcedNo != null -> (announcedNo + index) to "announce"
                numberingFromDuelStart && blobSerial != null -> blobSerial to "duel_start"
                else -> null to "unknown"
            }
            val capture = matchCapture(blobSerial, index == n - 1, n)
            // association 只说明“凭什么把这份胜负配给这份录像”，口径与官方参考包一致。
            val association = when {
                capture == null -> "none"
                gameNoSource == "announce" -> "STOC_CHAT/第N局公告 + replay game number"
                gameNoSource == "duel_start" -> "STOC_DUEL_START/MSG_START + replay game number"
                else -> "STOC_GAME_MSG/MSG_WIN + replay（局次未确认）"
            }
            if (capture != null) consume(capture)
            ReplayBlob(
                bytes = bytes,
                serial = blobSerial,
                gameNo = gameNo,
                gameNoSource = gameNoSource,
                capture = capture?.numbered(gameNo, gameNoSource),
                association = association,
            )
        }
        if (tailSerial != null) lastAssignedSerial = tailSerial
        if (announcedNo != null) announced = null
        return ReplayBatch(items)
    }

    /**
     * 找这份录像对应的胜负：只认同一局序号，绝不拿“最近一个还没配的胜负”顶替。
     * 序号未知时（没有 MSG_START），只在整批只有一份、且恰好一条待配胜负时才配。
     */
    private fun matchCapture(blobSerial: Int?, isTail: Boolean, batchSize: Int): GameCapture? {
        if (blobSerial != null) return pendingWins[blobSerial]
        if (!isTail) return null
        val unknown = pendingWins[0] ?: return null
        return if (batchSize == 1) unknown else null
    }

    private fun accepted(body: ByteArray): Boolean {
        if (body.isEmpty()) return false
        val type = body[0].toInt() and 0xFF
        if (type == YgoProtocol.MSG_DRAW) {
            // MSG_DRAW：player(1) + count(1) + count*4，服务端同样按这个公式校验
            return body.size >= 3 &&
                (body[1].toInt() and 0xFF) in 0..1 &&
                body.size == 3 + 4 * (body[2].toInt() and 0xFF)
        }
        return EVENT_LENGTHS[type] == body.size
    }

    private companion object {
        const val MAX_EVENTS = 100_000
        /** 待配胜负的池子上限：正常时序里最多只有一条，留余量防意外堆积。 */
        const val MAX_PENDING = 8
        const val MAX_GAME_NO = 99

        /** 服务端 `apply_live_evidence` / `watch.py` 认的定长消息（类型 → 正文长度）。 */
        val EVENT_LENGTHS = mapOf(
            YgoProtocol.MSG_WIN to 3,
            40 to 2,
            50 to 17,
            60 to 9,
            61 to 1,
            62 to 9,
            63 to 1,
            64 to 9,
            65 to 1,
            70 to 17,
        )
    }
}

internal fun ByteArray.toHexString(): String = joinToString("") { "%02x".format(it) }
