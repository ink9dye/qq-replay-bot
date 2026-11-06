package com.moecard.watch.ui.query

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.moecard.watch.net.AuthException
import com.moecard.watch.query.model.SearchResults
import com.moecard.watch.query.net.QuerySession
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 搜索：玩家（前百内，门户透传）/ 卡片（ygocdb 透传）/ 卡组（当前前百分布，≤30）。
 * 服务端 q ≤ 80 字，type 仅 player/card/deck。
 */
class SearchViewModel : ViewModel() {
    val state = MutableStateFlow<UiState>(UiState.Idle)
    val type = MutableStateFlow("player")

    private var searchJob: kotlinx.coroutines.Job? = null

    sealed interface UiState {
        object Idle : UiState
        object Loading : UiState
        data class Ready(val results: SearchResults) : UiState
        data class Failed(val message: String) : UiState
    }

    /**
     * 单类型单发 + 350ms 防抖：回车搜索立即发出；
     * 切 Tab 触发的重查走防抖，快速连点只发最后一条（服务端闸门只有 2 并发）。
     */
    fun search(q: String, immediate: Boolean = false) {
        val t = type.value
        val query = q.trim()
        if (query.isEmpty()) return
        searchJob?.cancel()
        searchJob = viewModelScope.launch {
            if (!immediate) kotlinx.coroutines.delay(350)
            state.value = UiState.Loading
            try {
                val res = withContext(Dispatchers.IO) { QuerySession.api().search(t, query) }
                state.value = UiState.Ready(res)
            } catch (e: AuthException) {
                QuerySession.expire()
            } catch (e: Exception) {
                state.value = UiState.Failed(e.message ?: "搜索失败")
            }
        }
    }
}

@Composable
fun SearchScreen(
    onOpenPlayer: (String) -> Unit,
    onOpenCard: (Long, String?) -> Unit,
    onOpenDeck: (String, Int) -> Unit,
) {
    val vm: SearchViewModel = viewModel()
    val state by vm.state.collectAsStateWithLifecycle()
    val type by vm.type.collectAsStateWithLifecycle()
    var query by rememberSaveable { mutableStateOf("") }

    Column(modifier = Modifier.fillMaxSize()) {
        OutlinedTextField(
            value = query,
            onValueChange = { query = it.take(80) },
            label = { Text("关键词（≤80字）") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
            keyboardActions = KeyboardActions(onSearch = { vm.search(query, immediate = true) }),
            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
        )
        TabRow(selectedTabIndex = listOf("player", "card", "deck").indexOf(type)) {
            listOf("player" to "玩家", "card" to "卡片", "deck" to "卡组").forEach { (t, label) ->
                Tab(
                    selected = type == t,
                    onClick = {
                        vm.type.value = t
                        if (query.isNotBlank() && vm.state.value != SearchViewModel.UiState.Idle) {
                            vm.search(query)
                        }
                    },
                    text = { Text(label) },
                )
            }
        }
        when (val s = state) {
            SearchViewModel.UiState.Idle -> {
                EmptyView("输入关键词开始搜索")
                // 官方接口支持任意玩家名，不限于前百——给个精确直达入口
                if (type == "player" && query.isNotBlank()) {
                    androidx.compose.material3.TextButton(
                        onClick = { onOpenPlayer(query.trim()) },
                        modifier = Modifier.padding(horizontal = 12.dp),
                    ) {
                        Text("按玩家名精确查看「${query.trim()}」→")
                    }
                }
            }
            SearchViewModel.UiState.Loading -> LoadingView("搜索中…")
            is SearchViewModel.UiState.Failed -> ErrorView(s.message) { vm.search(query, immediate = true) }
            is SearchViewModel.UiState.Ready -> {
                val r = s.results
                if (r.limited) {
                    Text(
                        "结果较多，仅显示前 30 条",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
                    )
                }
                LazyColumn(modifier = Modifier.fillMaxSize()) {
                    when (type) {
                        "player" -> items(r.players, key = { "p" + it.player }) { p ->
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable { onOpenPlayer(p.player) }
                                    .padding(horizontal = 16.dp, vertical = 10.dp),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Column(modifier = Modifier.weight(1f)) {
                                    Text(p.player, style = MaterialTheme.typography.bodyLarge)
                                    if (p.deckType != null) {
                                        Text(
                                            "#${p.rank} · ${p.deckType}",
                                            style = MaterialTheme.typography.labelSmall,
                                            color = MaterialTheme.colorScheme.outline,
                                            maxLines = 1,
                                            overflow = TextOverflow.Ellipsis,
                                        )
                                    }
                                }
                                Text(
                                    fmtNum(p.points),
                                    style = MaterialTheme.typography.bodyMedium,
                                    color = MaterialTheme.colorScheme.primary,
                                )
                            }
                        }
                        "card" -> items(r.cards, key = { "c" + it.id }) { c ->
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable { onOpenCard(c.id, c.name) }
                                    .padding(horizontal = 16.dp, vertical = 10.dp),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Column(modifier = Modifier.weight(1f)) {
                                    Text(c.name, style = MaterialTheme.typography.bodyLarge)
                                    if (c.types != null) {
                                        Text(
                                            c.types,
                                            style = MaterialTheme.typography.labelSmall,
                                            color = MaterialTheme.colorScheme.outline,
                                            maxLines = 1,
                                            overflow = TextOverflow.Ellipsis,
                                        )
                                    }
                                }
                                Text(
                                    c.id.toString(),
                                    style = MaterialTheme.typography.labelMedium,
                                    color = MaterialTheme.colorScheme.outline,
                                )
                            }
                        }
                        else -> items(r.decks, key = { "d" + it.name }) { d ->
                            Card(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(horizontal = 12.dp, vertical = 4.dp)
                                    .clickable { onOpenDeck(d.name, d.players) },
                            ) {
                                Row(
                                    modifier = Modifier.fillMaxWidth().padding(12.dp),
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    Text(d.name, style = MaterialTheme.typography.bodyLarge,
                                        modifier = Modifier.weight(1f))
                                    Text(
                                        "${d.players} 人在用 ›",
                                        style = MaterialTheme.typography.labelMedium,
                                        color = MaterialTheme.colorScheme.primary,
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

private fun fmtNum(v: Double): String =
    if (v == v.toLong().toDouble()) v.toLong().toString() else String.format("%.1f", v)
