package com.moecard.watch.ui.query

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.moecard.watch.net.AuthException
import com.moecard.watch.query.model.Subscription
import com.moecard.watch.query.net.QuerySession
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** 订阅管理：仅本人订阅；写入需服务端 writes_enabled + 账号 writes。 */
class SubsViewModel : ViewModel() {
    val state = MutableStateFlow<UiState>(UiState.Loading)

    sealed interface UiState {
        object Loading : UiState
        data class Ready(val items: List<Subscription>, val writes: Boolean) : UiState
        data class Failed(val message: String) : UiState
    }

    init {
        reload()
    }

    fun reload() {
        state.value = UiState.Loading
        viewModelScope.launch {
            try {
                val data = withContext(Dispatchers.IO) {
                    val subs = QuerySession.api().subscriptions()
                    val writes = QuerySession.session.value?.writes ?: false
                    subs to writes
                }
                state.value = UiState.Ready(data.first, data.second)
            } catch (e: AuthException) {
                QuerySession.expire()
            } catch (e: Exception) {
                state.value = UiState.Failed(e.message ?: "加载失败")
            }
        }
    }

    fun remove(player: String) {
        viewModelScope.launch {
            try {
                withContext(Dispatchers.IO) { QuerySession.api().unsubscribe(player) }
                reload()
            } catch (e: AuthException) {
                QuerySession.expire()
            } catch (e: Exception) {
                state.value = UiState.Failed(e.message ?: "退订失败")
            }
        }
    }
}

@Composable
fun SubsScreen(onOpenPlayer: (String) -> Unit) {
    val vm: SubsViewModel = viewModel()
    val state by vm.state.collectAsStateWithLifecycle()

    when (val s = state) {
        SubsViewModel.UiState.Loading -> LoadingView("正在拉取订阅…")
        is SubsViewModel.UiState.Failed -> ErrorView(s.message) { vm.reload() }
        is SubsViewModel.UiState.Ready -> {
            Column(Modifier.fillMaxSize()) {
                Card(
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
                ) {
                    Column(Modifier.fillMaxWidth().padding(12.dp)) {
                        Text("我的订阅 · ${s.items.size}", style = MaterialTheme.typography.titleSmall)
                        if (!s.writes) {
                            Text(
                                "当前账号无写入权限：可在玩家详情页订阅，但需要服务端开启 writes_enabled 且账号 writes 为真。",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.outline,
                            )
                        }
                    }
                }
                if (s.items.isEmpty()) {
                    EmptyView("还没有订阅；到玩家详情页打开订阅开关")
                } else {
                    LazyColumn {
                        items(s.items, key = { it.player }) { sub ->
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable { onOpenPlayer(sub.player) }
                                    .padding(horizontal = 16.dp, vertical = 10.dp),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Column(Modifier.weight(1f)) {
                                    Text(sub.player, style = MaterialTheme.typography.bodyLarge)
                                    Text(
                                        listOfNotNull(
                                            sub.rankTop?.let { "进前 $it 名通知" },
                                            if (sub.notify) "变化通知" else null,
                                        ).joinToString(" · ").ifBlank { "仅关注" },
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.outline,
                                    )
                                }
                                OutlinedButton(onClick = { vm.remove(sub.player) }) { Text("退订") }
                            }
                        }
                    }
                }
            }
        }
    }
}
