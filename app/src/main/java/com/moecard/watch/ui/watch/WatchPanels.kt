package com.moecard.watch.ui.watch

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.moecard.watch.model.WatchDevice
import com.moecard.watch.model.WatchStatus
import com.moecard.watch.ui.MainViewModel
import com.moecard.watch.ui.WatchSettingsSection
import com.moecard.watch.ui.formatLogTime
import kotlinx.coroutines.delay

private const val DEVICE_REFRESH_MS = 30_000L

/**
 * 当前在线设备：从观战调度服务器的 /watch-api/status 拉 workers 列表，
 * 页面打开期间每 30 秒自动刷新，也可手动点「刷新」。
 */
@Composable
fun DeviceListTab(vm: MainViewModel) {
    val state by vm.devices.collectAsStateWithLifecycle()

    LaunchedEffect(Unit) {
        while (true) {
            vm.refreshDevices()
            delay(DEVICE_REFRESH_MS)
        }
    }

    Column(Modifier.fillMaxSize()) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("当前在线设备", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
            TextButton(onClick = { vm.refreshDevices() }) { Text("刷新") }
        }
        when (val s = state) {
            MainViewModel.DeviceState.Idle,
            MainViewModel.DeviceState.Loading -> Column(
                Modifier.fillMaxSize(),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
            ) {
                CircularProgressIndicator()
                Spacer(Modifier.height(8.dp))
                Text("正在读取设备列表…", style = MaterialTheme.typography.labelMedium)
            }
            is MainViewModel.DeviceState.Failed -> EmptyHint(
                title = "无法读取在线设备",
                message = "${s.message}\n请到「设置 → 观战机器人」检查服务器地址与接口令牌后重试。",
            )
            is MainViewModel.DeviceState.Ready -> ReadyDevices(s.status, s.at)
        }
    }
}

@Composable
private fun ReadyDevices(status: WatchStatus, at: Long) {
    val online = status.onlineDevices
    val offline = status.offlineDevices
    Column(Modifier.fillMaxSize()) {
        Card(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp),
            colors = CardDefaults.outlinedCardColors(),
        ) {
            Column(Modifier.fillMaxWidth().padding(12.dp)) {
                Text(
                    "在线 ${online.size} 台" + if (offline.isNotEmpty()) " · 离线 ${offline.size} 台" else "",
                    style = MaterialTheme.typography.titleSmall,
                )
                Text(
                    "待认领 ${status.pending} · 已认领 ${status.claimed} · 更新于 ${formatLogTime(at)}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
        }
        Spacer(Modifier.height(8.dp))
        if (status.devices.isEmpty()) {
            EmptyHint("暂无设备", "服务器还没有任何观战终端上报心跳。")
        } else {
            LazyColumn(Modifier.fillMaxSize().padding(horizontal = 12.dp)) {
                items(status.devices, key = { it.name }) { d -> DeviceRow(d) }
            }
        }
    }
}

@Composable
private fun DeviceRow(d: WatchDevice) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Surface(
                color = if (d.online) Color(0xFF2E7D32) else MaterialTheme.colorScheme.outline,
                shape = CircleShape,
                modifier = Modifier.size(8.dp),
            ) {}
            Spacer(Modifier.size(10.dp))
            Column(Modifier.weight(1f)) {
                Text(d.name, style = MaterialTheme.typography.bodyLarge)
                Text(
                    buildString {
                        if (d.ip.isNotBlank()) append("IP ${d.ip} · ")
                        append("占用 ${d.active}/${d.capacity}")
                        if (d.seenAt > 0) {
                            append(" · 心跳 ")
                            append(formatLogTime((d.seenAt * 1000).toLong()))
                        }
                    },
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
            Text(
                if (d.online) "在线" else "离线",
                style = MaterialTheme.typography.labelMedium,
                color = if (d.online) Color(0xFF2E7D32) else MaterialTheme.colorScheme.outline,
            )
        }
    }
}

@Composable
private fun EmptyHint(title: String, message: String) {
    Column(
        Modifier.fillMaxSize().padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(title, style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(6.dp))
        Text(
            message,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.outline,
        )
    }
}

/**
 * 首次登录的助战机器人引导：先给操作提醒（QQ 机器人要令牌 / 注册 MC 平台账号），
 * 再复用设置页同一套「服务器 / 令牌 / 账号」表单，完成后返回首页。
 */
@Composable
fun WatchSetupScreen(vm: MainViewModel, onDone: () -> Unit) {
    Column(Modifier.fillMaxSize()) {
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 8.dp),
        ) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer),
            ) {
                Column(Modifier.fillMaxWidth().padding(14.dp)) {
                    Text("开启前需要准备两样东西", style = MaterialTheme.typography.titleSmall)
                    Spacer(Modifier.height(8.dp))
                    StepText(
                        "① 接口令牌",
                        "在 QQ 里找机器人发送「令牌分发」，拿到观战接口令牌；" +
                            "把它填到下面的「接口令牌」框。没有令牌时无法连接调度服务器。",
                    )
                    Spacer(Modifier.height(8.dp))
                    StepText(
                        "② 萌卡账号",
                        "还没有 MC 平台账号？用浏览器打开 mycard.moecube.com 注册后再回来。" +
                            "在下面「萌卡账号」里填写账号名与密码即可；一个账号同一时间观战一间房，账号数 = 并发路数。",
                    )
                    Spacer(Modifier.height(8.dp))
                    StepText(
                        "③ 服务器地址",
                        "保持默认地址即可；填好令牌和账号后，可以点「测试连接」确认服务器可达。",
                    )
                }
            }
            Spacer(Modifier.height(8.dp))
            WatchSettingsSection(vm)
            Spacer(Modifier.height(8.dp))
        }
        Surface(shadowElevation = 6.dp, modifier = Modifier.fillMaxWidth()) {
            Button(
                onClick = onDone,
                modifier = Modifier.fillMaxWidth().padding(12.dp),
            ) { Text("完成，返回首页") }
        }
    }
}

@Composable
private fun StepText(title: String, body: String) {
    Column {
        Text(title, style = MaterialTheme.typography.labelLarge)
        Text(
            body,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSecondaryContainer,
        )
    }
}
