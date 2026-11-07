package com.moecard.watch.ui.query

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.clickable
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.moecard.watch.net.AuthException
import com.moecard.watch.query.model.BuildData
import com.moecard.watch.query.model.DeckThumbs
import com.moecard.watch.query.model.RosterData
import com.moecard.watch.query.net.QuerySession
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 卡组详情：饼图切片 / 卡组搜索结果的落点。
 *  API 没有“卡组统计”端点，这里用现有数据拼出等价视图：
 *  top100 里使用该卡组的玩家列表（点进玩家看完整构筑）+ 排名最高使用者的构筑做参考。
 */
class DeckDetailViewModel : ViewModel() {

    val state = MutableStateFlow<UiState>(UiState.Loading)
    val reference = MutableStateFlow<RefState>(RefState.Idle)

    sealed interface UiState {
        object Loading : UiState
        data class Ready(
            val deckName: String,
            val usage: Int,
            val players: List<com.moecard.watch.query.model.RosterPerson>,
        ) : UiState

        data class Failed(val message: String) : UiState
    }

    sealed interface RefState {
        object Idle : RefState
        object Loading : RefState
        data class Ready(val player: String, val build: BuildData) : RefState
        data class Failed(val message: String) : RefState
    }

    private var loadedKey: String? = null

    fun load(deckName: String, usageHint: Int) {
        if (loadedKey == deckName) return
        loadedKey = deckName
        state.value = UiState.Loading
        reference.value = RefState.Idle
        viewModelScope.launch {
            try {
                val roster = withContext(Dispatchers.IO) { RosterCache.get() }
                val players = roster.people
                    .filter { it.deckType == deckName }
                    .sortedBy { it.rank }
                val usage = if (usageHint > 0) usageHint
                else roster.deckDistribution.firstOrNull { it.name == deckName }?.players
                    ?: players.size
                state.value = UiState.Ready(deckName, usage, players)
                // 懒加载参考构筑：排名第一的使用者
                players.firstOrNull()?.let { top ->
                    reference.value = RefState.Loading
                    try {
                        val build = withContext(Dispatchers.IO) { QuerySession.api().decks(top.player) }
                        reference.value = RefState.Ready(top.player, build)
                    } catch (e: AuthException) {
                        QuerySession.expire()
                    } catch (e: Exception) {
                        reference.value = RefState.Failed(e.message ?: "构筑加载失败")
                    }
                }
            } catch (e: AuthException) {
                QuerySession.expire()
            } catch (e: Exception) {
                state.value = UiState.Failed(e.message ?: "加载失败")
            }
        }
    }
}

/** top100 短缓存：饼图 → 卡组详情 → 玩家详情 一路点进来不重复拉榜单。 */
object RosterCache {
    private const val TTL = 60_000L
    private var cached: Pair<Long, RosterData>? = null

    fun get(): RosterData {
        val c = cached
        if (c != null && System.currentTimeMillis() - c.first < TTL) return c.second
        val data = QuerySession.api().top100()
        cached = System.currentTimeMillis() to data
        return data
    }
}

@Composable
fun DeckDetailScreen(deckName: String, usageHint: Int, onOpenPlayer: (String) -> Unit) {
    val vm: DeckDetailViewModel = viewModel(key = "deck-$deckName")
    LaunchedEffect(deckName) { vm.load(deckName, usageHint) }
    val state by vm.state.collectAsStateWithLifecycle()
    val reference by vm.reference.collectAsStateWithLifecycle()
    val ctx = LocalContext.current

    when (val s = state) {
        DeckDetailViewModel.UiState.Loading -> LoadingView("正在拉取卡组数据…")
        is DeckDetailViewModel.UiState.Failed -> ErrorView(s.message) { vm.load(deckName, usageHint) }
        is DeckDetailViewModel.UiState.Ready -> {
            Column(
                Modifier
                    .fillMaxSize()
                    .verticalScroll(rememberScrollState())
                    .padding(16.dp)
            ) {
                // 头部：代表卡图 + 名称 + 使用人数
                Row(verticalAlignment = Alignment.CenterVertically) {
                    val series = s.players.firstOrNull()?.series ?: emptyList()
                    val thumbCode = DeckThumbs.codeFor(ctx, deckName, series)
                    if (thumbCode != null) {
                        CardImage(
                            code = thumbCode,
                            contentScale = ContentScale.Fit,
                            modifier = Modifier
                                .height(120.dp)
                                .width(86.dp)
                                .clip(RoundedCornerShape(8.dp)),
                        )
                        Spacer(Modifier.width(12.dp))
                    }
                    Column {
                        Text(
                            deckName,
                            style = MaterialTheme.typography.headlineSmall,
                            fontWeight = FontWeight.Bold,
                        )
                        Text(
                            "前百 ${s.usage} 人在用",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.primary,
                        )
                    }
                }
                Spacer(Modifier.height(12.dp))

                // 使用玩家列表
                Text("使用该卡组的前百玩家", style = MaterialTheme.typography.titleMedium)
                Spacer(Modifier.height(4.dp))
                if (s.players.isEmpty()) {
                    Text(
                        "当前前百暂无人使用（榜单是快照，构筑识别覆盖有限）",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.outline,
                    )
                } else {
                    s.players.forEach { p ->
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .clickable { onOpenPlayer(p.player) }
                                .padding(vertical = 8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Text(
                                "#${p.rank}",
                                style = MaterialTheme.typography.bodyLarge,
                                fontWeight = FontWeight.Bold,
                                color = if (p.rank in 1..3) MaterialTheme.colorScheme.primary
                                else MaterialTheme.colorScheme.outline,
                                modifier = Modifier.width(48.dp),
                            )
                            Text(
                                p.player,
                                style = MaterialTheme.typography.bodyLarge,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                                modifier = Modifier.weight(1f),
                            )
                            Text(
                                "${fmt(p.points)} DP ›",
                                style = MaterialTheme.typography.labelMedium,
                                color = MaterialTheme.colorScheme.primary,
                            )
                        }
                    }
                }

                // 参考构筑（排名第一的使用者）
                Spacer(Modifier.height(8.dp))
                when (val r = reference) {
                    DeckDetailViewModel.RefState.Idle -> {}
                    DeckDetailViewModel.RefState.Loading -> LoadingView("正在加载参考构筑…")
                    is DeckDetailViewModel.RefState.Failed -> Text(
                        "参考构筑加载失败：${r.message}",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                    )
                    is DeckDetailViewModel.RefState.Ready -> {
                        Text(
                            "参考构筑（${r.player}，点击上方玩家可看其他人的构筑）",
                            style = MaterialTheme.typography.titleMedium,
                        )
                        Spacer(Modifier.height(4.dp))
                        if (r.build.cards.isEmpty()) {
                            Text(
                                "该玩家暂无采集到的构筑",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.outline,
                            )
                        } else {
                            DeckCardsList(r.build, deckLabel = deckName)
                        }
                    }
                }
                Spacer(Modifier.height(24.dp))
            }
        }
    }
}

private fun fmt(v: Double): String =
    if (v == v.toLong().toDouble()) v.toLong().toString() else String.format("%.1f", v)
