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
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
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
import androidx.lifecycle.viewmodel.compose.viewModel
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
