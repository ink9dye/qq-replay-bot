package com.moecard.watch.ui.tools

import android.app.Application
import android.net.Uri
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.moecard.watch.tools.CardInfo
import com.moecard.watch.tools.CardInfoRepo
import com.moecard.watch.tools.ToolDeck
import com.moecard.watch.tools.ToolDeckStore
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/** 两个计算器共用：把卡组里的卡号查成卡片数据（联网按需 + 本地缓存）。 */
class ToolCardsViewModel(app: Application) : AndroidViewModel(app) {

    sealed interface State {
        data object Idle : State
        data class Loading(val done: Int, val total: Int) : State
        data class Ready(val deck: ToolDeck, val infos: Map<Long, CardInfo>, val missing: List<Long>) : State
        data class Failed(val message: String) : State
    }

    private val _state = MutableStateFlow<State>(State.Idle)
    val state: StateFlow<State> = _state.asStateFlow()

    private var loadedFor: ToolDeck? = null
    private var job: Job? = null

    fun load(deck: ToolDeck, force: Boolean = false) {
        if (!force && deck == loadedFor && _state.value is State.Ready) return
        loadedFor = deck
        job?.cancel()
        job = viewModelScope.launch {
            _state.value = State.Loading(0, (deck.main + deck.extra).distinct().size)
            try {
                val result = CardInfoRepo.load(getApplication<Application>(), deck.main + deck.extra) { done, total ->
                    _state.value = State.Loading(done, total)
                }
                _state.value = State.Ready(deck, result.cards, result.missing)
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                _state.value = State.Failed(e.message ?: "卡片数据加载失败")
            }
        }
    }
}

/**
 * 卡组来源卡片：显示当前卡组，支持粘贴 YDK / 打开 .ydk 文件；
 * 也可以从玩家详情、卡组详情的构筑页点「小世界」「联立炮」直接带过来。
 */
@Composable
fun DeckSourceCard(deck: ToolDeck?, state: ToolCardsViewModel.State, onRetry: () -> Unit) {
    val ctx = LocalContext.current
    var showPaste by remember { mutableStateOf(false) }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        if (uri == null) return@rememberLauncherForActivityResult
        val text = try {
            ctx.contentResolver.openInputStream(uri)?.use { it.readBytes().toString(Charsets.UTF_8) }
        } catch (_: Exception) {
            null
        }
        val name = uri.lastPathSegment?.substringAfterLast('/')?.removeSuffix(".ydk") ?: "导入的卡组"
        val parsed = text?.let { ToolDeck.parseYdk(it, name) }
        if (parsed == null) {
            Toast.makeText(ctx, "没有读到卡号，确认一下是不是 .ydk 文件", Toast.LENGTH_LONG).show()
        } else {
            ToolDeckStore.set(parsed)
        }
    }

    Card(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp)) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            if (deck == null) {
                Text("还没有卡组", style = MaterialTheme.typography.titleSmall)
                Text(
                    "在玩家详情或卡组详情的「构筑」页点「小世界」「联立炮」可直接带入；也可以导入 YDK。",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            } else {
                Text(deck.label, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
                Text(
                    "主卡组 ${deck.main.size} 张 · 额外卡组 ${deck.extra.size} 张",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                when (state) {
                    is ToolCardsViewModel.State.Loading -> {
                        Spacer(Modifier.height(6.dp))
                        LinearProgressIndicator(
                            progress = { if (state.total == 0) 0f else state.done.toFloat() / state.total },
                            modifier = Modifier.fillMaxWidth(),
                        )
                        Text(
                            "正在查询卡片数据（百鸽）${state.done}/${state.total}",
                            style = MaterialTheme.typography.labelSmall,
                        )
                    }
                    is ToolCardsViewModel.State.Ready -> if (state.missing.isNotEmpty()) {
                        Text(
                            "有 ${state.missing.size} 种卡没查到数据（网络或新卡），计算时会跳过",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                        TextButton(onClick = onRetry) { Text("重新查询") }
                    }
                    is ToolCardsViewModel.State.Failed -> {
                        Text(state.message, color = MaterialTheme.colorScheme.error,
                            style = MaterialTheme.typography.labelSmall)
                        TextButton(onClick = onRetry) { Text("重试") }
                    }
                    ToolCardsViewModel.State.Idle -> Unit
                }
            }
            Spacer(Modifier.height(6.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                OutlinedButton(onClick = { showPaste = true }) { Text("粘贴 YDK") }
                OutlinedButton(onClick = { picker.launch(arrayOf("*/*")) }) { Text("打开 .ydk 文件") }
            }
        }
    }

    if (showPaste) {
        var text by remember { mutableStateOf("") }
        AlertDialog(
            onDismissRequest = { showPaste = false },
            title = { Text("粘贴 YDK") },
            text = {
                Column {
                    Text(
                        "支持 YGOMobile / YGOPro 导出的 .ydk 全文，或每行一个卡号的清单。",
                        style = MaterialTheme.typography.labelSmall,
                    )
                    Spacer(Modifier.height(6.dp))
                    OutlinedTextField(
                        value = text,
                        onValueChange = { text = it },
                        modifier = Modifier.fillMaxWidth().heightIn(min = 120.dp, max = 260.dp),
                        placeholder = { Text("#main\n89631139\n…") },
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Ascii),
                    )
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    val parsed = ToolDeck.parseYdk(text, "粘贴的卡组")
                    if (parsed == null) {
                        Toast.makeText(ctx, "没有识别到卡号", Toast.LENGTH_SHORT).show()
                    } else {
                        ToolDeckStore.set(parsed)
                        showPaste = false
                    }
                }) { Text("导入") }
            },
            dismissButton = { TextButton(onClick = { showPaste = false }) { Text("取消") } },
        )
    }
}

/** 带标签的整数步进器：[-] 值 [+]。 */
@Composable
fun NumberStepper(
    label: String,
    value: Int,
    range: IntRange,
    onChange: (Int) -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(modifier, verticalAlignment = Alignment.CenterVertically) {
        Text(label, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
        TextButton(onClick = { if (value > range.first) onChange(value - 1) }, enabled = value > range.first) {
            Text("−")
        }
        Text(
            value.toString(),
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.width(36.dp),
            textAlign = androidx.compose.ui.text.style.TextAlign.Center,
        )
        TextButton(onClick = { if (value < range.last) onChange(value + 1) }, enabled = value < range.last) {
            Text("+")
        }
    }
}
