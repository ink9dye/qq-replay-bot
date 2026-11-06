package com.moecard.watch.ui.query

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.moecard.watch.net.AuthException
import com.moecard.watch.query.model.PieData
import com.moecard.watch.query.net.QuerySession
import com.moecard.watch.ui.charts.PieChart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** MC 前百统计饼图（与门户前百快照共源；ROTK 为预留）。 */
class PieViewModel : ViewModel() {
    val state = MutableStateFlow<UiState>(UiState.Loading)

    sealed interface UiState {
        object Loading : UiState
        data class Ready(val data: PieData) : UiState
        data class Failed(val message: String) : UiState
    }

    init {
        reload()
    }

    fun reload() {
        state.value = UiState.Loading
        viewModelScope.launch {
            try {
                val data = withContext(Dispatchers.IO) { QuerySession.api().pie() }
                state.value = UiState.Ready(data)
            } catch (e: AuthException) {
                QuerySession.expire()
            } catch (e: Exception) {
                state.value = UiState.Failed(e.message ?: "加载失败")
            }
        }
    }
}

@Composable
fun PieScreen(onOpenDeck: (String, Int) -> Unit) {
    val vm: PieViewModel = viewModel()
    val state by vm.state.collectAsStateWithLifecycle()
    val scroll = rememberScrollState()

    when (val s = state) {
        PieViewModel.UiState.Loading -> LoadingView("正在拉取前百构筑分布…")
        is PieViewModel.UiState.Failed -> ErrorView(s.message) { vm.reload() }
        is PieViewModel.UiState.Ready -> {
            Column(Modifier.fillMaxSize().verticalScroll(scroll).padding(16.dp)) {
                Text("MC 前百构筑分布", style = MaterialTheme.typography.titleMedium)
                Text(
                    "覆盖 ${s.data.covered}/${s.data.current} 人 · 未识别 ${s.data.unidentified}\n点击卡组查看详情（使用玩家 / 参考构筑）",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                Spacer(Modifier.height(12.dp))
                PieChart(s.data.items, onSliceClick = { slice ->
                    onOpenDeck(slice.name, slice.count)
                })
                Spacer(Modifier.height(16.dp))
                ReservedCard(
                    "ROTK 饼图",
                    "另一数据源的前百构筑分布。查询接口目前只提供门户（MC）单一数据源。",
                )
            }
        }
    }
}
