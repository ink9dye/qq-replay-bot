package com.moecard.watch.ui.query

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import androidx.lifecycle.viewmodel.compose.viewModel
import com.moecard.watch.net.AuthException
import com.moecard.watch.query.net.QuerySession
import com.moecard.watch.ui.MainViewModel
import com.moecard.watch.ui.WatchSettingsSection

/**
 * 设置：查询服务器地址 / 自签信任开关 / 观战机器人（服务器、令牌、账号） / 会话信息 / 登出。
 */
@Composable
fun SettingsScreen(onLoggedOut: () -> Unit) {
    val session by QuerySession.session.collectAsStateWithLifecycle()
    var server by remember(QuerySession.server) { mutableStateOf(QuerySession.server) }
    var trust by remember(QuerySession.trustSelfSigned) { mutableStateOf(QuerySession.trustSelfSigned) }
    val scroll = rememberScrollState()
    val watchVm: MainViewModel = viewModel()

    Column(Modifier.fillMaxSize().verticalScroll(scroll).padding(16.dp)) {
        Text("查询服务器", style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(4.dp))
        OutlinedTextField(
            value = server,
            onValueChange = { server = it },
            label = { Text("服务器地址") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        Button(onClick = { QuerySession.setServer(server) }, modifier = Modifier.fillMaxWidth()) {
            Text("保存服务器地址")
        }
        Spacer(Modifier.height(8.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("接受自签证书", style = MaterialTheme.typography.bodyLarge)
                Text(
                    "服务器使用自签证书 + IP 直连时必须开启；仅对查询请求生效。换正式证书后可关闭。",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
            Switch(
                checked = trust,
                onCheckedChange = {
                    trust = it
                    QuerySession.setTrustSelfSigned(it)
                },
            )
        }

        Spacer(Modifier.height(20.dp))
        Text("观战机器人", style = MaterialTheme.typography.titleMedium)
        Text(
            "服务器地址、接口令牌与萌卡账号；与上方查询服务器相互独立。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
        )
        WatchSettingsSection(watchVm)

        Spacer(Modifier.height(16.dp))
        BindingSection()

        Spacer(Modifier.height(16.dp))
        CacheSection()

        Spacer(Modifier.height(16.dp))
        Text("当前会话", style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(4.dp))
        val s = session
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.outlinedCardColors(),
        ) {
            Column(Modifier.fillMaxWidth().padding(12.dp)) {
                if (s == null) {
                    Text("未登录", style = MaterialTheme.typography.bodyMedium)
                } else {
                    InfoRow("QQ 号", s.identity)
                    InfoRow("角色", s.role)
                    InfoRow("写权限", if (s.writes) "有" else "无")
                    InfoRow("access 到期", formatTime(s.accessExpiresAt))
                    InfoRow("refresh 到期", formatTime(s.refreshExpiresAt))
                }
            }
        }

        Spacer(Modifier.height(16.dp))
        Button(
            onClick = {
                QuerySession.logout()
                onLoggedOut()
            },
            colors = ButtonDefaults.buttonColors(
                containerColor = MaterialTheme.colorScheme.error,
                contentColor = Color.White,
            ),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("退出登录")
        }

        Spacer(Modifier.height(16.dp))
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.outlinedCardColors(),
        ) {
            Column(Modifier.fillMaxWidth().padding(12.dp)) {
                Text("数据来源", style = MaterialTheme.typography.titleSmall)
                Spacer(Modifier.height(4.dp))
                Text(
                    "玩家资料 / 对战历史 / DP 曲线：萌卡官方竞技场接口（sapi.moecube.com，实时）。\n" +
                        "前百 / 构筑 / 录像 / 订阅 / 饼图 / 卡片统计：自建查询服务器（需服务端启用 query）。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                Spacer(Modifier.height(4.dp))
                Text(
                    "观战机器人的服务器与账号已移到本页上方「观战机器人」中，与查询服务器相互独立。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
        }
    }
}

@Composable
private fun InfoRow(label: String, value: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
        Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.outline)
        Spacer(Modifier.weight(1f))
        Text(value, style = MaterialTheme.typography.bodyMedium)
    }
}

/** 绑定状态：整块自包含，登录后自动读一次。 */
private sealed interface BindingState {
    object Idle : BindingState
    object Loading : BindingState
    data class Ready(val player: String?) : BindingState
    data class Failed(val message: String) : BindingState
}

/**
 * 「我的 MC 绑定」：与 QQ 端「绑定mc」共用同一条记录（qq_bindings，user_id = QQ 号）。
 * 这里改绑会同时影响 QQ 端不带名字的默认查询对象，所以提示写清楚。
 */
@Composable
private fun BindingSection() {
    val session by QuerySession.session.collectAsStateWithLifecycle()
    var state by remember { mutableStateOf<BindingState>(BindingState.Idle) }
    var input by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var hint by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    LaunchedEffect(session?.identity) {
        if (session == null) {
            state = BindingState.Idle
            return@LaunchedEffect
        }
        state = BindingState.Loading
        try {
            val info = withContext(Dispatchers.IO) { QuerySession.api().binding() }
            state = BindingState.Ready(info.player)
            input = info.player.orEmpty()
        } catch (e: AuthException) {
            state = BindingState.Failed("登录已过期，请重新登录")
        } catch (e: Exception) {
            state = BindingState.Failed(e.message ?: "读取绑定失败")
        }
    }

    fun doBind() {
        val name = input.trim()
        if (name.isEmpty() || busy) return
        val wasBound = (state as? BindingState.Ready)?.player != null
        busy = true
        hint = null
        scope.launch {
            try {
                val r = withContext(Dispatchers.IO) { QuerySession.api().bindBinding(name) }
                state = BindingState.Ready(r.player)
                input = r.player
                hint = if (r.known) {
                    "已绑定 ${r.player}" + if (wasBound && r.changed) "（已改绑）" else ""
                } else {
                    "已绑定 ${r.player}，但最近 14 天没收录到这个名字，可能打错了"
                }
            } catch (e: AuthException) {
                hint = "登录已过期，请重新登录"
            } catch (e: Exception) {
                hint = e.message ?: "绑定失败"
            } finally {
                busy = false
            }
        }
    }

    fun doUnbind() {
        if (busy) return
        busy = true
        hint = null
        scope.launch {
            try {
                val removed = withContext(Dispatchers.IO) { QuerySession.api().unbindBinding() }
                state = BindingState.Ready(null)
                input = ""
                hint = if (removed) "已解绑" else "本来就没有绑定"
            } catch (e: AuthException) {
                hint = "登录已过期，请重新登录"
            } catch (e: Exception) {
                hint = e.message ?: "解绑失败"
            } finally {
                busy = false
            }
        }
    }

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Text("我的 MC 绑定", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(4.dp))
            when (val s = state) {
                BindingState.Idle -> Text(
                    "登录后可绑定 MC 玩家名",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                BindingState.Loading -> Text(
                    "正在读取绑定…",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                is BindingState.Failed -> Text(
                    s.message,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
                is BindingState.Ready -> {
                    InfoRow("当前绑定", s.player ?: "未绑定")
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(
                        value = input,
                        onValueChange = { input = it },
                        label = { Text(if (s.player == null) "MC 玩家名" else "改为其它玩家名") },
                        singleLine = true,
                        enabled = !busy,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(8.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Button(
                            onClick = { doBind() },
                            enabled = !busy && input.trim().isNotEmpty(),
                        ) { Text(if (s.player == null) "绑定" else "改绑") }
                        Spacer(Modifier.padding(horizontal = 4.dp))
                        Button(
                            onClick = { doUnbind() },
                            enabled = !busy && s.player != null,
                            colors = ButtonDefaults.buttonColors(
                                containerColor = MaterialTheme.colorScheme.error,
                                contentColor = Color.White,
                            ),
                        ) { Text("解绑") }
                        if (busy) {
                            Spacer(Modifier.padding(horizontal = 4.dp))
                            CircularProgressIndicator(modifier = Modifier.height(18.dp).padding(2.dp))
                        }
                    }
                }
            }
            val h = hint
            if (h != null) {
                Spacer(Modifier.height(6.dp))
                Text(h, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.primary)
            }
            Spacer(Modifier.height(6.dp))
            Text(
                "与 QQ 端「绑定mc」是同一条记录：在 App 改绑，QQ 发「历史」不带名字也会查这个玩家。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
    }
}

/**
 * 本地缓存：构筑 / 换备各缓存 5 分钟、前百 60 秒。
 * 服务器算得慢的时候用它清一下，避免一直读到旧结果。
 */
@Composable
private fun CacheSection() {
    var cleared by remember { mutableStateOf(false) }
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Text("本地缓存", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(4.dp))
            Text(
                "构筑 / 换备各保留 5 分钟，前百榜单 60 秒。数据更新了但页面还是旧的，可以清一次。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
            Spacer(Modifier.height(8.dp))
            Button(onClick = {
                PlayerTabCache.clear()
                RosterCache.clear()
                cleared = true
            }) {
                Text(if (cleared) "已清空" else "清空本地缓存")
            }
        }
    }
}
