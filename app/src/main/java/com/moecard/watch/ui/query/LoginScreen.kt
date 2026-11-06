package com.moecard.watch.ui.query

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.moecard.watch.query.net.QuerySession
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 登录：QQ 号 + 一次性激活码（默认 24h 有效）。
 * 激活码来源：私聊机器人「开通查询」或管理员签发；服务端重复开通会 409。
 */
class LoginViewModel : ViewModel() {
    val busy = MutableStateFlow(false)
    val error = MutableStateFlow<String?>(null)

    fun login(account: String, secret: String, server: String, onOk: () -> Unit) {
        if (busy.value) return
        val acc = account.trim()
        if (!Regex("^\\d{5,20}$").matches(acc)) {
            error.value = "请输入 5~20 位数字的 QQ 号"
            return
        }
        if (secret.isBlank()) {
            error.value = "请输入激活码"
            return
        }
        busy.value = true
        error.value = null
        viewModelScope.launch {
            try {
                val pair = withContext(Dispatchers.IO) {
                    QuerySession.setServer(server)
                    QuerySession.api().login(acc, secret)
                }
                QuerySession.applyLogin(pair)
                onOk()
            } catch (e: Exception) {
                error.value = e.message ?: "登录失败"
            } finally {
                busy.value = false
            }
        }
    }
}

@Composable
fun LoginScreen(onLoggedIn: () -> Unit) {
    val vm: LoginViewModel = viewModel()
    val busy by vm.busy.collectAsStateWithLifecycle()
    val error by vm.error.collectAsStateWithLifecycle()

    var account by rememberSaveable { mutableStateOf("") }
    var secret by rememberSaveable { mutableStateOf("") }
    var server by rememberSaveable { mutableStateOf(QuerySession.server) }
    var showAdvanced by rememberSaveable { mutableStateOf(false) }
    val scroll = rememberScrollState()

    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(scroll).padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text("戏王版权局", style = MaterialTheme.typography.headlineMedium)
        Text("萌卡竞技数据查询", style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.outline)
        Spacer(Modifier.height(24.dp))

        OutlinedTextField(
            value = account,
            onValueChange = { account = it.filter(Char::isDigit).take(20) },
            label = { Text("QQ 号") },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = secret,
            onValueChange = { secret = it },
            label = { Text("激活码") },
            visualTransformation = PasswordVisualTransformation(),
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(12.dp))

        if (showAdvanced) {
            OutlinedTextField(
                value = server,
                onValueChange = { server = it },
                label = { Text("查询服务器") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
        }

        if (error != null) {
            Text(error!!, color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall)
            Spacer(Modifier.height(8.dp))
        }

        Button(
            onClick = { vm.login(account, secret, server, onLoggedIn) },
            enabled = !busy,
            modifier = Modifier.fillMaxWidth(),
        ) {
            if (busy) {
                CircularProgressIndicator(modifier = Modifier.height(16.dp), strokeWidth = 2.dp)
                Spacer(Modifier.height(4.dp))
            }
            Text(if (busy) " 登录中…" else "登录")
        }

        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            TextButton(onClick = { showAdvanced = !showAdvanced }) {
                Text(if (showAdvanced) "收起高级选项" else "高级选项")
            }
        }

        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.outlinedCardColors(),
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                Text("没有激活码？", style = MaterialTheme.typography.titleSmall)
                Spacer(Modifier.height(4.dp))
                Text(
                    "已有使用权限的用户，私聊 QQ 机器人发送「开通查询」即可领取一次性激活码（24 小时内有效）；" +
                        "群聊不发放。激活码仅能使用一次，登录后 access 令牌 24 小时、refresh 令牌 180 天。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
        }
    }
}
