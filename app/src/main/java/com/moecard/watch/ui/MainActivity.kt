package com.moecard.watch.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.moecard.watch.ui.theme.MoeCardTheme
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import com.moecard.watch.R

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: android.os.Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1001)
        }
        setContent {
            MoeCardTheme {
                // 查询端为应用主体（登录判定 + 抽屉导航），观战机器人保留为其中一个入口
                AppRoot()
            }
        }
    }
}

/** 观战机器人设置 / 账号表单：设置页与首次登录引导页共用（不含滚动，由外层容器决定）。 */
@Composable
fun WatchSettingsSection(vm: MainViewModel, modifier: Modifier = Modifier) {
    val cfg by vm.config.collectAsStateWithLifecycle()
    val running by vm.isRunning.collectAsStateWithLifecycle()
    val ctx = LocalContext.current
    var accName by remember { mutableStateOf("") }
    var accPwd by remember { mutableStateOf("") }

    Column(modifier = modifier.fillMaxWidth().padding(vertical = 8.dp)) {
        OutlinedTextField(value = cfg.server, onValueChange = vm::setServer,
            label = { Text("服务器地址") }, modifier = Modifier.fillMaxWidth(),
            placeholder = { Text("例如 http://101.201.108.188:8787") })
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(value = cfg.token, onValueChange = vm::setToken,
            label = { Text("接口令牌") }, modifier = Modifier.fillMaxWidth(),
            placeholder = { Text("向管理员索取") })
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(value = cfg.clientName, onValueChange = vm::setClientName,
            label = { Text("客户端名称（同一令牌下唯一）") }, modifier = Modifier.fillMaxWidth())

        Spacer(Modifier.height(16.dp))
        Text("萌卡账号（每个账号同时观战一间房，账号数 = 并发路数）", style = MaterialTheme.typography.titleSmall)
        Text(
            "注意：第三方自动观战存在封号风险，建议只使用小号，不要用主账号。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.error,
        )
        Spacer(Modifier.height(8.dp))
        cfg.accounts.forEachIndexed { i, a ->
            Card(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                colors = CardDefaults.outlinedCardColors()) {
                Row(modifier = Modifier.fillMaxWidth().padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text(a.name, style = MaterialTheme.typography.bodyLarge)
                        Text("密码已加密保存", style = MaterialTheme.typography.labelSmall)
                    }
                    OutlinedButton(onClick = { vm.removeAccount(i) }) { Text("删除") }
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(value = accName, onValueChange = { accName = it },
            label = { Text("新增账号名") }, modifier = Modifier.fillMaxWidth())
        Spacer(Modifier.height(6.dp))
        OutlinedTextField(value = accPwd, onValueChange = { accPwd = it },
            label = { Text("密码") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        Spacer(Modifier.height(6.dp))
        Button(onClick = {
            vm.addAccount(accName, accPwd)
            accName = ""; accPwd = ""
        }, modifier = Modifier.fillMaxWidth()) { Text("＋ 添加账号") }

        Spacer(Modifier.height(16.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("在本机保留录像（可用 YGOPro 打开）", modifier = Modifier.weight(1f))
            Switch(checked = cfg.keepLocal, onCheckedChange = vm::setKeepLocal)
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("启动后自动开始观战", modifier = Modifier.weight(1f))
            Switch(checked = cfg.autoStart, onCheckedChange = vm::setAutoStart)
        }

        Spacer(Modifier.height(16.dp))
        Row {
            OutlinedButton(onClick = {
                vm.testConnection { res ->
                    val msg = if (res.isSuccess) {
                        val i = res.getOrThrow()
                        "连接成功 · 在线 ${i.onlineWorkers} · 待认领 ${i.pending}"
                    } else "连接失败：${res.exceptionOrNull()?.message}"
                    Toast.makeText(ctx, msg, Toast.LENGTH_LONG).show()
                }
            }) { Text("测试连接") }
            Spacer(Modifier.weight(1f))
            Button(
                onClick = {
                    if (running) {
                        vm.stopWatching()
                    } else {
                        requestBatteryExemption(ctx)
                        vm.startWatching()
                    }
                },
                enabled = cfg.accounts.isNotEmpty(),
            ) {
                if (running) {
                    Icon(Icons.Filled.Stop, contentDescription = null)
                    Text(" 停止观战")
                } else {
                    Icon(Icons.Filled.PlayArrow, contentDescription = null)
                    Text(" 开始观战")
                }
            }
        }
        if (cfg.accounts.isEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text("请先添加至少一个萌卡账号再开始。", style = MaterialTheme.typography.labelSmall)
        }
    }
}

@Composable
fun TasksTab(vm: MainViewModel) {
    val jobs by vm.jobs.collectAsStateWithLifecycle()
    val stats by vm.stats.collectAsStateWithLifecycle()
    val conn by vm.connectionInfo.collectAsStateWithLifecycle()
    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Card(modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp)) {
            Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
                Text("在线 ${conn?.onlineWorkers ?: 0} · 待认领 ${conn?.pending ?: 0} · 活跃 ${stats.active}/${stats.capacity}",
                    style = MaterialTheme.typography.titleSmall)
                Text("已保存 ${stats.saved} · 已上传 ${stats.uploaded} · 任务 ${stats.jobs} · 失败 ${stats.failed}",
                    style = MaterialTheme.typography.labelSmall)
            }
        }
        LazyColumn {
            items(jobs) { job ->
                Card(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                    colors = CardDefaults.outlinedCardColors()) {
                    Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
                        Text(job.title, style = MaterialTheme.typography.bodyLarge)
                        Text("账号 ${job.account} · ${job.players.joinToString(" vs ")}", style = MaterialTheme.typography.labelSmall)
                        Text("${job.status} · 保存 ${job.saved} / 上传 ${job.uploaded}", style = MaterialTheme.typography.labelSmall)
                        if (job.detail.isNotEmpty()) {
                            Text(job.detail, style = MaterialTheme.typography.labelSmall)
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun LogsTab(vm: MainViewModel) {
    val logs by vm.logs.collectAsStateWithLifecycle()
    val listState = rememberLazyListState()

    // 进入日志页就定位到最新一条，不用每次手动往下滑。
    // （LogsTab 随页签切换会重建，所以每次点开「日志」都会重新执行）
    LaunchedEffect(Unit) {
        if (logs.isNotEmpty()) listState.scrollToItem(logs.lastIndex)
    }

    // 新日志到达时：用户本来就停在底部则继续跟随；已经往上翻看历史则不去打扰。
    // 用最后一条日志做 key（日志上限 500 条，到顶后 size 就不再变化了）。
    LaunchedEffect(logs.lastOrNull()) {
        if (logs.isEmpty()) return@LaunchedEffect
        val lastVisible = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: -1
        if (lastVisible >= logs.size - 2) {
            listState.animateScrollToItem(logs.lastIndex)
        }
    }

    LazyColumn(state = listState, modifier = Modifier.fillMaxSize().padding(8.dp)) {
        items(logs) { entry ->
            val color = when (entry.level) {
                "error" -> androidx.compose.ui.graphics.Color(0xFFB00020)
                "warn" -> androidx.compose.ui.graphics.Color(0xFFB26A00)
                "success" -> androidx.compose.ui.graphics.Color(0xFF007A00)
                else -> MaterialTheme.colorScheme.onSurface
            }
            Row(modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
                Text(formatLogTime(entry.time), style = MaterialTheme.typography.labelSmall,
                    fontFamily = FontFamily.Monospace, fontSize = 11.sp, color = MaterialTheme.colorScheme.outline)
                Spacer(Modifier.height(4.dp))
                Text(entry.message, style = MaterialTheme.typography.labelSmall,
                    fontFamily = FontFamily.Monospace, fontSize = 11.sp, color = color,
                    modifier = Modifier.padding(start = 8.dp))
            }
            HorizontalDivider()
        }
    }
}

private val logTimeFormat = java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault())

/** 把日志时间戳格式化为 HH:mm:ss。 */
fun formatLogTime(timeMs: Long): String = logTimeFormat.format(java.util.Date(timeMs))

/**
 * 申请「忽略电池优化」豁免：Doze 省电模式会在息屏且静止时限制网络与后台任务，
 * 导致 TCP 长连接被掐断。若尚未豁免，则跳转系统设置页引导用户开启。
 */
private fun requestBatteryExemption(ctx: android.content.Context) {
    val pm = ctx.getSystemService(android.content.Context.POWER_SERVICE) as android.os.PowerManager
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !pm.isIgnoringBatteryOptimizations(ctx.packageName)) {
        try {
            val intent = android.content.Intent(
                android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
            ).apply { data = android.net.Uri.parse("package:${ctx.packageName}") }
            ctx.startActivity(intent)
        } catch (_: Exception) {
            // 部分 ROM 不支持该 intent，忽略即可，WakeLock/WifiLock 仍会兜底
        }
    }
}
