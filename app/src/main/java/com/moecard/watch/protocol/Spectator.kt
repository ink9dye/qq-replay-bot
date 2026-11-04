package com.moecard.watch.protocol

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException

/**
 * 一次观战会话的结果。
 *
 * [connected] / [frames] 用于上层判断“值不值得重连”：
 *  - connected=false：TCP 没连上（房间大概率已关闭 / 网络还没恢复）；
 *  - connected=true 且掉线：房间还在，重连一次就能接着看，必须重连而不是结束任务。
 */
data class SpectateResult(
    val outcome: String, // saved / noreplay / retry / gone / lost
    val replaysSaved: Int = 0,
    val connected: Boolean = false,
    val frames: Int = 0,
)

/**
 * 以观战身份连入萌卡/ygopro 房间，读取 STOC_REPLAY 录像字节并通过 onReplay 回调交出去。
 * 纯 java.net 实现，不依赖 Android，便于单元测试。
 *
 * 只负责“一次”连接：断线后的重连由 WatchClient 的会话循环负责（这样可以在重连前刷新
 * 萌卡密钥、更换账号槽位、做退避）。
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
        var connected = false
        var frames = 0
        var outcome = "retry"
        try {
            onLog("连接 $host:$port，观战 $nickname", "info")
            val s = Socket()
            socket = s
            // 显式 connect 超时：断网/切网时不会一直卡在 75s 的系统默认超时上
            s.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
            // 开启 TCP keepalive，配合半开连接（切网后信号已断但对端无感知）自动探测
            try { s.keepAlive = true } catch (_: Exception) { }
            try { s.tcpNoDelay = true } catch (_: Exception) { }
            s.soTimeout = 20_000
            val out = s.getOutputStream()
            out.write(YgoProtocol.playerInfoPacket(nickname))
            out.write(YgoProtocol.joinPacket(version, password))
            out.flush()
            connected = true
            onConnected()
            val input = s.getInputStream()

            while (true) {
                if (aborted) {
                    outcome = "lost"
                    break
                }
                // 对齐 PC watch.py watch_read_timeout：对局结束短等录像 25s；
                // 收到过数据说明房间活跃，长等 120s；完全没数据 30s 后重连。
                s.soTimeout = when {
                    duelEnded -> 25_000
                    receivedAny -> 120_000
                    else -> 30_000
                }
                val frame = try {
                    YgoProtocol.readFrame(input).also { if (it != null) { receivedAny = true; frames++ } }
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
            SpectateResult(outcome, replaysSaved, connected, frames)
        } catch (e: IOException) {
            onLog("连接失败：${e.message}", "error")
            SpectateResult(if (aborted) "lost" else "retry", replaysSaved, connected, frames)
        } finally {
            try {
                socket?.close()
            } catch (_: Exception) {
            }
            socket = null
        }
    }

    private companion object {
        /** 单个 TCP 连接超时：切网瞬间连不上时快速失败，交给上层退避重连。 */
        const val CONNECT_TIMEOUT_MS = 12_000
    }
}
