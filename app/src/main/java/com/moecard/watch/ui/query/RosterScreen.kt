package com.moecard.watch.ui.query

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.moecard.watch.net.AuthException
import com.moecard.watch.query.model.DeckThumbs
import com.moecard.watch.query.model.RosterData
import com.moecard.watch.query.model.RosterPerson
import com.moecard.watch.query.net.QuerySession
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** 前百玩家：卡组缩略卡墙（两列）。每张卡 = 卡组代表卡图 + 排名 + 玩家名 + DP。 */
class RosterViewModel : ViewModel() {
    val state = MutableStateFlow<UiState>(UiState.Loading)

    sealed interface UiState {
        object Loading : UiState
        /** deckImages：卡组名 → 饼图选定的代表卡卡号（与「前百统计」饼图完全一致）。 */
        data class Ready(val data: RosterData, val deckImages: Map<String, Long>) : UiState
        data class Failed(val message: String) : UiState
    }

    init {
        reload()
    }

    fun reload() {
        state.value = UiState.Loading
        viewModelScope.launch {
            try {
                val data = withContext(Dispatchers.IO) { QuerySession.api().top100() }
                // 与饼图共用同一份代表卡选择；取不到时回退到本地映射
                val pie = withContext(Dispatchers.IO) {
                    runCatching { QuerySession.api().pie() }.getOrNull()
                }
                val deckImages = pie?.pie?.outer
                    ?.mapNotNull { d -> d.image?.let { d.name to it } }
                    ?.toMap()
                    .orEmpty()
                state.value = UiState.Ready(data, deckImages)
            } catch (e: AuthException) {
                QuerySession.expire()
            } catch (e: Exception) {
                state.value = UiState.Failed(e.message ?: "加载失败")
            }
        }
    }
}

@Composable
fun RosterScreen(onOpenPlayer: (String) -> Unit) {
    val vm: RosterViewModel = viewModel()
    val state by vm.state.collectAsStateWithLifecycle()

    when (val s = state) {
        RosterViewModel.UiState.Loading -> LoadingView("正在拉取前百榜单…")
        is RosterViewModel.UiState.Failed -> ErrorView(s.message) { vm.reload() }
        is RosterViewModel.UiState.Ready -> {
            val data = s.data
            Column(modifier = Modifier.fillMaxSize()) {
                Text(
                    "MC前百 · ${data.people.size}人 · 覆盖 ${data.covered}/${data.current} · 未识别 ${data.unidentified}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp),
                )
                if (data.people.isEmpty()) {
                    EmptyView("榜单为空")
                } else {
                    LazyVerticalGrid(
                        columns = GridCells.Fixed(2),
                        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                        modifier = Modifier.fillMaxSize(),
                    ) {
                        items(data.people, key = { it.player }) { p ->
                            RosterTile(p, s.deckImages, onOpenPlayer)
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun RosterTile(
    p: RosterPerson,
    deckImages: Map<String, Long>,
    onOpenPlayer: (String) -> Unit,
) {
    val ctx = LocalContext.current
    val deckName = p.deckType
    // 优先用饼图同款代表卡（服务端 image），没有再走本地映射
    val thumbCode = deckName?.let { deckImages[it] } ?: DeckThumbs.codeFor(ctx, deckName, p.series)
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onOpenPlayer(p.player) },
    ) {
        Column {
            Box {
                if (thumbCode != null) {
                    ArtworkImage(
                        code = thumbCode,
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(96.dp),
                    )
                } else {
                    // 没有卡组映射 / 未观测：色块兜底
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(96.dp)
                            .background(
                                Brush.verticalGradient(
                                    listOf(
                                        MaterialTheme.colorScheme.surfaceVariant,
                                        MaterialTheme.colorScheme.surface,
                                    )
                                )
                            ),
                        contentAlignment = Alignment.Center,
                    ) {
                        Text(
                            deckName ?: "未观测",
                            style = MaterialTheme.typography.labelLarge,
                            color = MaterialTheme.colorScheme.outline,
                        )
                    }
                }
                // 排名角标
                Surface(
                    color = if (p.rank in 1..3) MaterialTheme.colorScheme.primary
                    else Color(0xAA000000),
                    shape = RoundedCornerShape(bottomEnd = 8.dp),
                    modifier = Modifier.align(Alignment.TopStart),
                ) {
                    Text(
                        "#${p.rank}",
                        style = MaterialTheme.typography.labelMedium,
                        color = Color.White,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
                    )
                }
            }
            Column(Modifier.fillMaxWidth().padding(horizontal = 10.dp, vertical = 8.dp)) {
                Text(
                    p.player,
                    style = MaterialTheme.typography.bodyLarge,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Row {
                    Text(
                        deckName ?: "未观测",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f),
                    )
                    Text(
                        "${fmtPoints(p.points)} DP",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.primary,
                        fontWeight = FontWeight.Bold,
                    )
                }
            }
        }
    }
}

private fun fmtPoints(v: Double): String =
    if (v == v.toLong().toDouble()) v.toLong().toString() else String.format("%.1f", v)
