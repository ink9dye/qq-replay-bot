package com.moecard.watch.protocol

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException
import java.net.Socket
import java.net.SocketTimeoutException

data class SpectateResult(
    val outcome: String, // saved / noreplay / retry / gone
    val replaysSaved: Int,
)

/**
 * 以观战身份连入萌卡/ygopro 房间，读取 STOC_REPLAY 录像字节并通过 onReplay 回调交出去。
 * 纯 java.net 实现，不依赖 Android，便于单元测试。
 */
class Spectator(
    private val host: String,
    private val port: Int,
    private val version: Int = YgoProtocol.DEFAULT_VERSION,
    private val nickname: String,
    private val password: String,
    private val onReplay: suspend (ByteArray) -> Unit,
    private val onLog: (String, String) -> Unit = { _, _ -> },
    private val onConnected: () -> Unit = {},
) {
    @Volatile private var duelEnded = false
    @Volatile private var replaysSaved = 0
    @Volatile private var receivedAny = false
    @Volatile private var socket: Socket? = null
    /** 网络切换 / 系统杀进程时由外部调用，立即断开当前连接以触发快速重连。 */
    @Volatile var aborted = false

    /** 强制中断当前连接（用于网络切换后立即重连，避免等超时）。 */
    fun abort() {
        aborted = true
        try { socket?.close() } catch (_: Exception) { }
    }

    suspend fun run(): SpectateResult = withContext(Dispatchers.IO) {
        socket = null
        try {
            onLog("连接 $host:$port，观战 $nickname", "info")
            socket = Socket(host, port)
            socket!!.soTimeout = 20_000
            val out = socket!!.getOutputStream()
            out.write(YgoProtocol.playerInfoPacket(nickname))
            out.write(YgoProtocol.joinPacket(version, password))
            out.flush()
            onConnected()
            val input = socket!!.getInputStream()

            var outcome = "retry"
            while (true) {
                if (aborted) {
                    outcome = "lost"
                    break
                }
                // 对齐 PC watch.py watch_read_timeout：对局结束短等录像 25s；
                // 收到过数据说明房间活跃，长等 120s；完全没数据 30s 后重连。
                socket!!.soTimeout = when {
                    duelEnded -> 25_000
                    receivedAny -> 120_000
                    else -> 30_000
                }
                val frame = try {
                    YgoProtocol.readFrame(input).also { if (it != null) receivedAny = true }
                } catch (e: SocketTimeoutException) {
                    outcome = if (duelEnded) (if (replaysSaved > 0) "saved" else "noreplay") else "retry"
                    break
                } catch (e: IOException) {
                    outcome = if (duelEnded && replaysSaved > 0) "saved" else "retry"
                    break
                }
                if (frame == null) {
                    outcome = if (replaysSaved > 0) "saved" else "noreplay"
                    break
                }
                val (proto, body) = frame
                when (proto) {
                    YgoProtocol.STOC_DUEL_START -> duelEnded = false
                    YgoProtocol.STOC_DUEL_END -> {
                        duelEnded = true
                        onLog("决斗结束，等待录像", "info")
                    }
                    YgoProtocol.STOC_REPLAY -> {
                        val files = ReplaySplitter.split(body)
                        val list = if (files.isEmpty() && body.isNotEmpty()) listOf(body) else files
                        for (blob in list) {
                            onReplay(blob)
                            replaysSaved++
                        }
                    }
                    YgoProtocol.STOC_TYPE_CHANGE -> {
                        val playerType = if (body.isNotEmpty()) (body[0].toInt() and 0xFF) else 0
                        if (playerType != YgoProtocol.NETPLAYER_OBSERVER) {
                            out.write(YgoProtocol.buildFrame(YgoProtocol.CTOS_HS_TOOBSERVER))
                            out.flush()
                        }
                    }
                    else -> { /* 其余协议号忽略 */ }
                }
            }
            SpectateResult(outcome, replaysSaved)
        } catch (e: IOException) {
            onLog("连接失败：${e.message}", "error")
            SpectateResult("retry", replaysSaved)
        } finally {
            try {
                socket?.close()
            } catch (_: Exception) {
            }
            socket = null
        }
    }
}
