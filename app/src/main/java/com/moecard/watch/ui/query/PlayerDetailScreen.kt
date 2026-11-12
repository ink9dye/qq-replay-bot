package com.moecard.watch.ui.query

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.widget.Toast
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.FilterChip
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Tab
import androidx.compose.material3.ScrollableTabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
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
import com.moecard.watch.query.model.ArenaMatch
import com.moecard.watch.query.model.ArenaProfile
import com.moecard.watch.query.model.BuildCard
import com.moecard.watch.query.model.BuildData
import com.moecard.watch.query.model.CardDetail
import com.moecard.watch.query.model.ReplayGame
import com.moecard.watch.query.model.ReplayMatch
import com.moecard.watch.query.model.ReplaySearch
import com.moecard.watch.query.model.SidePlan
import com.moecard.watch.query.model.dpCurve
import com.moecard.watch.query.model.resultFor
import com.moecard.watch.query.net.CardTextApi
import com.moecard.watch.query.net.MoeCardApi
import com.moecard.watch.query.net.QuerySession
import com.moecard.watch.ui.charts.LineChart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 玩家详情：双数据源。
 *  官方（sapi.moecube.com）——当前 DP / 排名 / 胜率、DP 曲线（最近10局）、对战历史（可翻页）；
 *  自建查询 API——构筑、录像、订阅开关。自建数据全部懒加载：进对应页签才发请求
 *  （服务端查询闸门只有 2 并发，不与其他屏抢）。
 */
private const val REPLAY_PAGE_SIZE = 30

/** 录像检索条件；胜负是客户端过滤（录像库没有 outcome 参数）。 */
data class ReplayFilters(
    val opponent: String = "",
    val deck: String = "",
    val outcome: String = "all", // all / win / loss
    val from: String = "",
    val to: String = "",
)

class PlayerDetailViewModel : ViewModel() {
    private val api = MoeCardApi()

    val official = MutableStateFlow<OfficialState>(OfficialState.Loading)
    val decks = MutableStateFlow<QueryState<BuildData?>>(QueryState.Idle)
    val sidePlan = MutableStateFlow<QueryState<SidePlan?>>(QueryState.Idle)
    val replaySearch = MutableStateFlow<QueryState<ReplaySearch?>>(QueryState.Idle)
    val replayFilters = MutableStateFlow(ReplayFilters())
    val replayLoadingMore = MutableStateFlow(false)
    val replayExhausted = MutableStateFlow(false)
    val subscribed = MutableStateFlow<Boolean?>(null) // null = 未查过
    val subscribeHint = MutableStateFlow<String?>(null)
    val busy = MutableStateFlow(false)
    val loadingMore = MutableStateFlow(false)
    val historyExhausted = MutableStateFlow(false) // 官方接口没有更多了

    sealed interface OfficialState {
        object Loading : OfficialState
        data class Ready(val profile: ArenaProfile, val matches: List<ArenaMatch>) : OfficialState
        data class Failed(val message: String) : OfficialState
    }

    sealed interface QueryState<out T> {
        object Idle : QueryState<Nothing>
        object Loading : QueryState<Nothing>
        data class Ready<T>(val data: T) : QueryState<T>
        data class Failed(val message: String) : QueryState<Nothing>
    }

    private var loadedPlayer: String? = null
    private var nextPage = 3 // 初始历史已拉 1~2 页

    fun load(player: String) {
        if (loadedPlayer == player) return
        loadedPlayer = player
        official.value = OfficialState.Loading
        decks.value = QueryState.Idle
        sidePlan.value = QueryState.Idle
        replaySearch.value = QueryState.Idle
        replayFilters.value = ReplayFilters()
        replayExhausted.value = false
        replayPage = 1
        subscribed.value = null
        subscribeHint.value = null
        nextPage = 3
        historyExhausted.value = false

        viewModelScope.launch {
            try {
                val data = withContext(Dispatchers.IO) {
                    val profile = api.profile(player)
                    val matches = api.history(player)
                    profile to matches
                }
                official.value = OfficialState.Ready(data.first, data.second)
            } catch (e: Exception) {
                official.value = OfficialState.Failed(e.message ?: "官方接口加载失败")
            }
        }
    }

    /**
     * 懒加载：进入换备页签时才拉。单次 1~3 s（服务端要算构筑 + 换边局），
     * 失败单独重试，不牵连其它页签。
     */
    fun loadSidePlan(player: String) {
        if (sidePlan.value !is QueryState.Idle) return
        sidePlan.value = QueryState.Loading
        viewModelScope.launch {
            try {
                val data = withContext(Dispatchers.IO) { QuerySession.api().sidePlan(player) }
                sidePlan.value = QueryState.Ready(data)
            } catch (e: AuthException) {
                QuerySession.expire()
            } catch (e: Exception) {
                sidePlan.value = QueryState.Failed(e.message ?: "换备加载失败")
            }
        }
    }

    fun retrySidePlan(player: String) {
        sidePlan.value = QueryState.Idle
        loadSidePlan(player)
    }

    /** 懒加载：进入构筑页签时才拉。 */
    fun loadDecks(player: String) {
        if (decks.value !is QueryState.Idle) return
        decks.value = QueryState.Loading
        viewModelScope.launch {
            try {
                val data = withContext(Dispatchers.IO) { QuerySession.api().decks(player) }
                decks.value = QueryState.Ready(data)
            } catch (e: AuthException) {
                QuerySession.expire()
            } catch (e: Exception) {
                decks.value = QueryState.Failed(e.message ?: "构筑加载失败")
            }
        }
    }

    /**
     * 录像检索（录像库 v2）：按对局返回，一场 = 1~3 局。
     * 条件：对手 / 卡组（双方任一方）/ 胜负（服务端没有该参数，客户端过滤）/ 日期区间。
     */
    fun searchReplays(player: String, filters: ReplayFilters = replayFilters.value) {
        replayPlayer = player
        replayFilters.value = filters
        replayPage = 1
        replayExhausted.value = false
        replaySearch.value = QueryState.Loading
        viewModelScope.launch {
            fetchReplays(player, filters, page = 1, append = false)
        }
    }

    fun setReplayFilters(filters: ReplayFilters) {
        replayFilters.value = filters
    }

    fun loadMoreReplays() {
        val player = replayPlayer ?: return
        if (replayLoadingMore.value || replayExhausted.value) return
        replayLoadingMore.value = true
        viewModelScope.launch {
            fetchReplays(player, replayFilters.value, page = replayPage + 1, append = true)
            replayLoadingMore.value = false
        }
    }

    suspend fun downloadReplay(game: ReplayGame): ByteArray =
        withContext(Dispatchers.IO) { QuerySession.replayApi().downloadYrp(game.download) }

    private var replayPlayer: String? = null
    private var replayPage = 1

    private suspend fun fetchReplays(
        player: String,
        filters: ReplayFilters,
        page: Int,
        append: Boolean,
    ) {
        try {
            val result = withContext(Dispatchers.IO) {
                QuerySession.replayApi().matches(
                    player = player,
                    opponent = filters.opponent.ifBlank { null },
                    deck = filters.deck.ifBlank { null },
                    from = filters.from.ifBlank { null },
                    to = filters.to.ifBlank { null },
                    page = page,
                    pageSize = REPLAY_PAGE_SIZE,
                )
            }
            val items = if (filters.outcome == "all") result.items
            else result.items.filter { it.resultFor(player) == filters.outcome }
            val previous = if (append) {
                (replaySearch.value as? QueryState.Ready)?.data?.items.orEmpty()
            } else {
                emptyList()
            }
            val seen = previous.mapTo(HashSet()) { it.key }
            val merged = previous + items.filter { seen.add(it.key) }
            replaySearch.value = QueryState.Ready(
                result.copy(items = merged)
            )
            replayPage = page
            replayExhausted.value = result.items.size < REPLAY_PAGE_SIZE
        } catch (e: Exception) {
            replayExhausted.value = false
            if (append) {
                replayLoadingMore.value = false
            } else {
                replaySearch.value = QueryState.Failed(e.message ?: "录像库请求失败")
            }
        }
    }

    /**
     * 历史翻页：每次多拉两页（100 局），按 (start,end) 去重合并；
     * 新拉结果为空即视为拉完。
     */
    fun loadMoreHistory(player: String) {
        val cur = official.value as? OfficialState.Ready ?: return
        if (loadingMore.value || historyExhausted.value) return
        loadingMore.value = true
        viewModelScope.launch {
            try {
                val more = withContext(Dispatchers.IO) {
                    api.historyFrom(player, nextPage, 2)
                }
                if (more.isEmpty()) {
                    historyExhausted.value = true
                } else {
                    nextPage += 2
                    val seen = HashSet(cur.matches.map { it.startTime * 1000_003L + it.endTime })
                    // 翻页拿到的是更早的对局，追加后必须重新按开始时间排序，
                    // 否则 HistoryTab 的 asReversed() 会把新旧顺序打乱。
                    val merged = (cur.matches + more.filter {
                        seen.add(it.startTime * 1000_003L + it.endTime)
                    }).sortedBy { it.startTime }
                    official.value = OfficialState.Ready(cur.profile, merged)
                    if (more.size < 100) historyExhausted.value = true
                }
            } catch (e: Exception) {
                // 翻页失败不打断列表，下次再试
            } finally {
                loadingMore.value = false
            }
        }
    }

    /**
     * 订阅开关：状态未知（还没查过 / 上次失败）时先查当前订阅再执行；
     * 按期望值语义，403 给出专门的权限提示。
     */
    fun toggleSubscribe(player: String, desired: Boolean) {
        if (busy.value) return
        busy.value = true
        subscribeHint.value = null
        viewModelScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    var current = subscribed.value
                    if (current == null) {
                        current = QuerySession.api().subscriptions().any { it.player == player }
                        subscribed.value = current
                    }
                    if (desired != current) {
                        if (desired) {
                            QuerySession.api().subscribe(player, null, false)
                        } else {
                            QuerySession.api().unsubscribe(player)
                        }
                        subscribed.value = desired
                    }
                }
            } catch (e: AuthException) {
                QuerySession.expire()
            } catch (e: Exception) {
                subscribeHint.value = e.message ?: "操作失败"
            } finally {
                busy.value = false
            }
        }
    }
}

@Composable
fun PlayerDetailScreen(player: String) {
    val vm: PlayerDetailViewModel = viewModel(key = "player-$player")
    LaunchedEffect(player) { vm.load(player) }
    var tab by rememberSaveable { mutableStateOf(0) }

    Column(modifier = Modifier.fillMaxSize()) {
        val official by vm.official.collectAsStateWithLifecycle()
        val subscribed by vm.subscribed.collectAsStateWithLifecycle()
        val hint by vm.subscribeHint.collectAsStateWithLifecycle()
        val busy by vm.busy.collectAsStateWithLifecycle()
        Card(modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp)) {
            Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        player, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold,
                        modifier = Modifier.weight(1f),
                    )
                    Column(horizontalAlignment = Alignment.End) {
                        Text(
                            "订阅", style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.outline,
                        )
                        Switch(
                            checked = subscribed == true,
                            enabled = !busy,
                            onCheckedChange = { vm.toggleSubscribe(player, it) },
                        )
                    }
                }
                if (subscribed == null && hint == null) {
                    Text(
                        "点开关时查询订阅状态",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                    )
                }
                if (hint != null) {
                    Text(hint!!, style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.error)
                }
            }
        }
        ScrollableTabRow(selectedTabIndex = tab, edgePadding = 0.dp) {
            listOf("概览", "DP曲线", "对战历史", "构筑", "换备", "录像").forEachIndexed { i, label ->
                Tab(selected = tab == i, onClick = { tab = i }, text = { Text(label) })
            }
        }
        when (tab) {
            0 -> OverviewTab(official)
            1 -> DpChartTab(official)
            2 -> HistoryTab(
                vm,
                player,
                official,
                onOpenReplay = { match ->
                    val day = dayKey(match.startTime)
                    val filters = ReplayFilters(opponent = match.opponent, from = day, to = day)
                    vm.searchReplays(player, filters)
                    tab = 5
                },
            )
            3 -> {
                LaunchedEffect(player) { vm.loadDecks(player) }
                DecksTab(vm, player)
            }
            4 -> {
                LaunchedEffect(player) { vm.loadSidePlan(player) }
                SidePlanTab(vm, player)
            }
            else -> {
                LaunchedEffect(player) {
                    if (vm.replaySearch.value is PlayerDetailViewModel.QueryState.Idle) {
                        vm.searchReplays(player)
                    }
                }
                ReplaysTab(vm, player)
            }
        }
    }
}

@Composable
private fun OverviewTab(state: PlayerDetailViewModel.OfficialState) {
    val scroll = rememberScrollState()
    when (state) {
        PlayerDetailViewModel.OfficialState.Loading -> LoadingView("正在查询萌卡官方数据…")
        is PlayerDetailViewModel.OfficialState.Failed -> Column(
            Modifier.fillMaxSize().verticalScroll(scroll).padding(16.dp)
        ) {
            ErrorView(state.message)
            Text(
                "官方数据来自 sapi.moecube.com，与查询服务器无关；可稍后重试。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
        is PlayerDetailViewModel.OfficialState.Ready -> {
            val p = state.profile
            Column(Modifier.fillMaxSize().verticalScroll(scroll).padding(16.dp)) {
                Row {
                    StatCard("当前DP", fmt(p.pt), Modifier.weight(1f))
                    StatCard("竞技排名", if (p.arenaRank > 0) "${p.arenaRank}" else "—", Modifier.weight(1f))
                    StatCard("胜率", "${p.winRate}%", Modifier.weight(1f))
                }
                Spacer(Modifier.height(8.dp))
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.outlinedCardColors(),
                ) {
                    Column(Modifier.fillMaxWidth().padding(12.dp)) {
                        Text("竞技场战绩", style = MaterialTheme.typography.titleSmall)
                        Spacer(Modifier.height(4.dp))
                        Text(
                            "总场 ${p.athleticAll} · 胜 ${p.athleticWin} · 负 ${p.athleticLose} · 平 ${p.athleticDraw}",
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        Text(
                            "EXP ${fmt(p.exp)}（排名 ${if (p.expRank > 0) p.expRank else "—"}）· 娱乐场 ${p.entertainAll} 战 ${p.entertainWin} 胜",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.outline,
                        )
                    }
                }
                // 最近一战：详情页第一眼就能看到最近结果
                state.matches.lastOrNull()?.let { latest ->
                    Spacer(Modifier.height(8.dp))
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.outlinedCardColors(),
                    ) {
                        Column(Modifier.fillMaxWidth().padding(12.dp)) {
                            Text("最近一战", style = MaterialTheme.typography.titleSmall)
                            Spacer(Modifier.height(4.dp))
                            MatchRow(latest)
                        }
                    }
                }
                Spacer(Modifier.height(8.dp))
                Text(
                    "数据来源：萌卡官方竞技场接口（实时）。",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
        }
    }
}

@Composable
private fun StatCard(label: String, value: String, modifier: Modifier = Modifier) {
    Card(modifier = modifier.padding(horizontal = 4.dp)) {
        Column(
            Modifier.fillMaxWidth().padding(vertical = 12.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(value, style = MaterialTheme.typography.headlineSmall, color = MaterialTheme.colorScheme.primary)
            Text(label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline)
        }
    }
}

@Composable
private fun DpChartTab(state: PlayerDetailViewModel.OfficialState) {
    val scroll = rememberScrollState()
    Column(Modifier.fillMaxSize().verticalScroll(scroll).padding(16.dp)) {
        when (state) {
            PlayerDetailViewModel.OfficialState.Loading -> LoadingView("正在拉取对战历史…")
            is PlayerDetailViewModel.OfficialState.Failed -> ErrorView(state.message)
            is PlayerDetailViewModel.OfficialState.Ready -> {
                Text("DP 变化（最近 10 局）", style = MaterialTheme.typography.titleSmall)
                Spacer(Modifier.height(8.dp))
                LineChart(dpCurve(state.matches.takeLast(10)))
                Spacer(Modifier.height(8.dp))
                Text(
                    "口径与积分走势一致：首局赛前分 + 每局赛后分（仅竞技对局）。完整历史见「对战历史」页。",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
        }
    }
}

@Composable
private fun HistoryTab(
    vm: PlayerDetailViewModel,
    player: String,
    state: PlayerDetailViewModel.OfficialState,
    onOpenReplay: (ArenaMatch) -> Unit,
) {
    when (state) {
        PlayerDetailViewModel.OfficialState.Loading -> LoadingView("正在拉取对战历史…")
        is PlayerDetailViewModel.OfficialState.Failed -> ErrorView(state.message)
        is PlayerDetailViewModel.OfficialState.Ready -> {
            val loadingMore by vm.loadingMore.collectAsStateWithLifecycle()
            val exhausted by vm.historyExhausted.collectAsStateWithLifecycle()
            val matches = state.matches.asReversed() // 最新在前
            if (matches.isEmpty()) {
                EmptyView("暂无竞技对局记录")
            } else {
                LazyColumn(Modifier.fillMaxSize()) {
                    items(matches.size) { i ->
                        Column(
                            Modifier.fillMaxWidth()
                                .clickable { onOpenReplay(matches[i]) }
                                .padding(horizontal = 16.dp, vertical = 8.dp)
                        ) {
                            MatchRow(matches[i])
                            Text(
                                "点按查这场的录像",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.outline,
                            )
                            HorizontalDivider()
                        }
                    }
                    item(key = "load-more") {
                        Column(
                            Modifier.fillMaxWidth().padding(vertical = 12.dp),
                            horizontalAlignment = Alignment.CenterHorizontally,
                        ) {
                            when {
                                loadingMore -> CircularProgressIndicator(modifier = Modifier.height(22.dp), strokeWidth = 2.dp)
                                exhausted -> Text(
                                    "已加载全部（${matches.size} 局）",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.outline,
                                )
                                else -> OutlinedButton(onClick = { vm.loadMoreHistory(player) }) {
                                    Text("加载更多")
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

/** 单行对局：胜负 + 对手 + ΔDP + 时间。 */
@Composable
private fun MatchRow(m: ArenaMatch) {
    val delta = m.ptAfter - m.ptBefore
    val deltaText = (if (delta >= 0) "+" else "") + fmt(delta)
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            when (m.won) {
                true -> "胜"
                false -> "负"
                null -> "平"
            },
            style = MaterialTheme.typography.titleSmall,
            fontWeight = FontWeight.Bold,
            color = when (m.won) {
                true -> MaterialTheme.colorScheme.primary
                false -> MaterialTheme.colorScheme.error
                null -> MaterialTheme.colorScheme.outline
            },
            modifier = Modifier.width(32.dp),
        )
        Column(Modifier.weight(1f)) {
            Text(m.opponent, style = MaterialTheme.typography.bodyLarge)
            Text(
                formatTime(m.endTime),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
        Column(horizontalAlignment = Alignment.End) {
            Text(
                deltaText, style = MaterialTheme.typography.bodyMedium,
                color = if (delta >= 0) MaterialTheme.colorScheme.primary
                else MaterialTheme.colorScheme.error,
            )
            Text(
                "${fmt(m.ptAfter)} DP",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
    }
}

@Composable
private fun DecksTab(vm: PlayerDetailViewModel, player: String) {
    val state by vm.decks.collectAsStateWithLifecycle()
    when (val s = state) {
        PlayerDetailViewModel.QueryState.Idle -> LoadingView()
        PlayerDetailViewModel.QueryState.Loading -> LoadingView("正在拉取构筑（查询服务器）…")
        is PlayerDetailViewModel.QueryState.Failed -> Column(
            Modifier.fillMaxSize().padding(16.dp)
        ) {
            ErrorView(s.message)
            Text(
                "构筑数据来自自建查询服务器，需要 query 功能已启用。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
        is PlayerDetailViewModel.QueryState.Ready -> {
            val data = s.data
            if (data == null || data.cards.isEmpty()) {
                EmptyView("暂无构筑数据（该玩家可能未被观战采集覆盖）")
            } else {
                Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
                    DeckCardsList(data, deckLabel = data.person?.deckType ?: player)
                }
            }
        }
    }
}

/** 构筑卡片列表（主/额外/副卡组分组），玩家详情与卡组详情共用。 */
@Composable
fun DeckCardsList(data: BuildData, deckLabel: String? = null) {
    var selected by remember { mutableStateOf<BuildCard?>(null) }
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    Column(Modifier.fillMaxWidth().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            CompletenessChip("主卡组", data.mainComplete)
            CompletenessChip("额外", data.extraComplete)
            CompletenessChip("副卡组", data.sideComplete)
            Spacer(Modifier.weight(1f))
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            TextButton(onClick = {
                scope.launch {
                    val msg = openYdkInYgomobile(ctx, deckLabel ?: "卡组", data)
                    Toast.makeText(ctx, msg, Toast.LENGTH_LONG).show()
                }
            }, contentPadding = PaddingValues(horizontal = 8.dp)) {
                Text("导入 YGOMobile", maxLines = 1)
            }
        }
        Spacer(Modifier.height(8.dp))
        val byPartition = data.cards.groupBy { it.partition }
        listOf(
            "main" to "主卡组",
            "extra" to "额外卡组",
            "side" to "副卡组",
        ).forEach { (key, label) ->
            // 分区即使一张都没采集到也照样展示，避免「副卡组不完整 → 整块消失」
            val cards = byPartition[key].orEmpty()
            Card(
                modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                colors = CardDefaults.outlinedCardColors(),
            ) {
                Column(Modifier.fillMaxWidth().padding(12.dp)) {
                    Text(
                        "$label · ${cards.sumOf { it.copies }} 张",
                        style = MaterialTheme.typography.titleSmall,
                    )
                    if (cards.isEmpty()) {
                        Spacer(Modifier.height(4.dp))
                        Text(
                            "尚未采集到这一分区（观战覆盖不足），下次对局后可能补全",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.outline,
                        )
                        return@Column
                    }
                    Spacer(Modifier.height(8.dp))
                    cards.sortedByDescending { it.copies }.chunked(3).forEach { row ->
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            row.forEach { card ->
                                DeckCardTile(
                                    card = card,
                                    modifier = Modifier.weight(1f),
                                    onClick = { selected = card },
                                )
                            }
                            repeat(3 - row.size) {
                                Spacer(Modifier.weight(1f))
                            }
                        }
                        Spacer(Modifier.height(8.dp))
                    }
                }
            }
        }
    }
    selected?.let { card ->
        CardEffectDialog(card = card, onDismiss = { selected = null })
    }
}

/** 单张卡：卡图 + 数量角标 + 卡名；点开看效果。 */
@Composable
private fun DeckCardTile(card: BuildCard, modifier: Modifier, onClick: () -> Unit) {
    Column(
        modifier
            .clip(RoundedCornerShape(6.dp))
            .clickable(onClick = onClick),
    ) {
        Box(Modifier.fillMaxWidth().aspectRatio(59f / 86f)) {
            CardImage(
                code = card.code,
                modifier = Modifier.fillMaxSize().clip(RoundedCornerShape(6.dp)),
                contentScale = ContentScale.Crop,
            )
            Surface(
                color = Color(0xCC000000),
                shape = RoundedCornerShape(bottomStart = 6.dp),
                modifier = Modifier.align(Alignment.TopEnd),
            ) {
                Text(
                    "×${card.copies}",
                    style = MaterialTheme.typography.labelSmall,
                    color = Color.White,
                    modifier = Modifier.padding(horizontal = 5.dp, vertical = 1.dp),
                )
            }
        }
        Text(
            card.name ?: "#${card.code}",
            style = MaterialTheme.typography.labelSmall,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = 3.dp),
        )
    }
}

private sealed interface CardInfoState {
    object Loading : CardInfoState
    data class Ready(val detail: CardDetail) : CardInfoState
    data class Failed(val message: String) : CardInfoState
}

/**
 * 卡片效果小弹窗：卡图 + 卡名 + 类型 + 效果。
 * 效果文本先走 ygocdb（约 0.3 秒），失败再回退自建 `/query-api/card/<code>`；
 * 同一张卡走内存缓存，失败不影响已显示的卡名/卡图，可单独重试。
 */
@Composable
private fun CardEffectDialog(card: BuildCard, onDismiss: () -> Unit) {
    var attempt by remember(card.code) { mutableStateOf(0) }
    var state by remember(card.code) { mutableStateOf<CardInfoState>(CardInfoState.Loading) }
    LaunchedEffect(card.code, attempt) {
        CardDetailCache.get(card.code)?.let {
            state = CardInfoState.Ready(it)
            return@LaunchedEffect
        }
        state = CardInfoState.Loading
        try {
            val detail = withContext(Dispatchers.IO) {
                // ygocdb 效果文本约 0.3 秒；失败/缺卡再走自建 /card（现网 6~8 秒）
                CardTextApi.fetch(card.code) ?: QuerySession.api().card(card.code)
            }
            CardDetailCache.put(card.code, detail)
            state = CardInfoState.Ready(detail)
        } catch (e: AuthException) {
            QuerySession.expire()
            state = CardInfoState.Failed("登录已过期，请重新登录")
        } catch (e: Exception) {
            state = CardInfoState.Failed(e.message ?: "效果加载失败")
        }
    }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(card.name ?: "卡片 #${card.code}") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                CardImage(
                    code = card.code,
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(220.dp)
                        .clip(RoundedCornerShape(8.dp)),
                )
                Spacer(Modifier.height(8.dp))
                Text(
                    "卡号 ${card.code}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                when (val s = state) {
                    CardInfoState.Loading -> Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.padding(top = 8.dp),
                    ) {
                        CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                        Spacer(Modifier.width(8.dp))
                        Text("正在读取卡片效果…", style = MaterialTheme.typography.bodySmall)
                    }
                    is CardInfoState.Failed -> Column(Modifier.padding(top = 8.dp)) {
                        Text(
                            s.message,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                        TextButton(onClick = { attempt++ }) { Text("重试") }
                    }
                    is CardInfoState.Ready -> {
                        val c = s.detail
                        c.types?.takeIf { it.isNotBlank() }?.let {
                            Spacer(Modifier.height(4.dp))
                            Text(
                                it,
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.outline,
                            )
                        }
                        if (!c.pdesc.isNullOrBlank()) {
                            Spacer(Modifier.height(8.dp))
                            Text("灵摆效果", style = MaterialTheme.typography.titleSmall)
                            Text(c.pdesc, style = MaterialTheme.typography.bodySmall)
                        }
                        if (!c.desc.isNullOrBlank()) {
                            Spacer(Modifier.height(8.dp))
                            Text(
                                if (!c.pdesc.isNullOrBlank()) "怪兽效果" else "卡片效果",
                                style = MaterialTheme.typography.titleSmall,
                            )
                            Text(c.desc, style = MaterialTheme.typography.bodySmall)
                        }
                        if (c.desc.isNullOrBlank() && c.pdesc.isNullOrBlank()) {
                            Spacer(Modifier.height(8.dp))
                            Text(
                                "该卡暂无效果文本",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.outline,
                            )
                        }
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) { Text("关闭") }
        },
    )
}

@Composable
private fun CompletenessChip(label: String, complete: Boolean) {
    Card(modifier = Modifier.padding(horizontal = 4.dp)) {
        Text(
            "$label ${if (complete) "完整" else "不完整"}",
            style = MaterialTheme.typography.labelSmall,
            color = if (complete) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.outline,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp),
        )
    }
}

@Composable
private fun ReplaysTab(vm: PlayerDetailViewModel, player: String) {
    val filters by vm.replayFilters.collectAsStateWithLifecycle()
    val state by vm.replaySearch.collectAsStateWithLifecycle()
    val loadingMore by vm.replayLoadingMore.collectAsStateWithLifecycle()
    val exhausted by vm.replayExhausted.collectAsStateWithLifecycle()
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var showAuth by remember { mutableStateOf(false) }

    Column(Modifier.fillMaxSize()) {
        ReplayFilterCard(
            filters = filters,
            onChange = vm::setReplayFilters,
            onSearch = { vm.searchReplays(player, filters) },
            onAuth = { showAuth = true },
        )
        when (val s = state) {
            PlayerDetailViewModel.QueryState.Idle,
            PlayerDetailViewModel.QueryState.Loading -> LoadingView("正在检索录像…")
            is PlayerDetailViewModel.QueryState.Failed -> Column(
                Modifier.fillMaxSize().padding(16.dp)
            ) {
                ErrorView(s.message)
                Text(
                    "录像库（/replay-api，默认 http://服务器:8787）与查询服务是两套；" +
                        "令牌用 QQ 给机器人发「令牌分发」获取。",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                TextButton(onClick = { showAuth = true }) { Text("填写录像库地址 / 令牌") }
            }
            is PlayerDetailViewModel.QueryState.Ready -> {
                val data = s.data
                if (data == null || data.items.isEmpty()) {
                    EmptyView("录像库保留 14 天；没有符合条件的对局")
                } else {
                    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
                        item(key = "count") {
                            Text(
                                "共 ${data.total} 场，已加载 ${data.items.size} 场" +
                                    if (filters.outcome != "all") "（胜负为本地筛选）" else "",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.outline,
                                modifier = Modifier.padding(vertical = 6.dp),
                            )
                        }
                        items(data.items, key = { it.key }) { match ->
                            ReplayMatchCard(
                                match = match,
                                player = player,
                                onImport = { game ->
                                    scope.launch {
                                        val tip = Toast.makeText(ctx, "正在下载录像…", Toast.LENGTH_SHORT)
                                        tip.show()
                                        try {
                                            val bytes = vm.downloadReplay(game)
                                            Toast.makeText(
                                                ctx,
                                                openYrpInYgomobile(ctx, game.name, bytes),
                                                Toast.LENGTH_LONG,
                                            ).show()
                                        } catch (e: Exception) {
                                            Toast.makeText(
                                                ctx, "下载失败：${e.message}", Toast.LENGTH_LONG,
                                            ).show()
                                        }
                                    }
                                },
                                onSave = { game ->
                                    scope.launch {
                                        try {
                                            val bytes = vm.downloadReplay(game)
                                            val name = game.name.ifBlank { "replay.yrp" }
                                                .let { if (it.endsWith(".yrp")) it else "$it.yrp" }
                                            val saved = withContext(Dispatchers.IO) {
                                                saveToDownloads(ctx, name, bytes)
                                            }
                                            Toast.makeText(ctx, "已保存：$saved", Toast.LENGTH_LONG).show()
                                        } catch (e: Exception) {
                                            Toast.makeText(
                                                ctx, "下载失败：${e.message}", Toast.LENGTH_LONG,
                                            ).show()
                                        }
                                    }
                                },
                            )
                        }
                        item(key = "more") {
                            Box(Modifier.fillMaxWidth().padding(12.dp), Alignment.Center) {
                                if (loadingMore) {
                                    CircularProgressIndicator(Modifier.size(22.dp))
                                } else if (!exhausted) {
                                    OutlinedButton(onClick = vm::loadMoreReplays) { Text("加载更多") }
                                } else {
                                    Text(
                                        "已经到底了",
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.outline,
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    if (showAuth) {
        var server by remember { mutableStateOf(QuerySession.replayServer) }
        var token by remember { mutableStateOf(QuerySession.replayToken) }
        AlertDialog(
            onDismissRequest = { showAuth = false },
            title = { Text("录像库连接") },
            text = {
                Column {
                    Text(
                        "录像库是独立服务：地址默认 http://服务器IP:8787，令牌是 QQ「令牌分发」给的 wt_ 令牌。",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                    )
                    OutlinedTextField(
                        value = server,
                        onValueChange = { server = it },
                        label = { Text("地址") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                    )
                    OutlinedTextField(
                        value = token,
                        onValueChange = { token = it },
                        label = { Text("wt_ 令牌") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                    )
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    QuerySession.setReplayServer(server)
                    QuerySession.setReplayToken(token)
                    showAuth = false
                    vm.searchReplays(player, filters)
                }) { Text("保存并检索") }
            },
            dismissButton = {
                TextButton(onClick = { showAuth = false }) { Text("取消") }
            },
        )
    }
}

@Composable
private fun ReplayFilterCard(
    filters: ReplayFilters,
    onChange: (ReplayFilters) -> Unit,
    onSearch: () -> Unit,
    onAuth: () -> Unit,
) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = filters.opponent,
                    onValueChange = { onChange(filters.copy(opponent = it)) },
                    label = { Text("对手") },
                    singleLine = true,
                    modifier = Modifier.weight(1f),
                )
                OutlinedTextField(
                    value = filters.deck,
                    onValueChange = { onChange(filters.copy(deck = it)) },
                    label = { Text("卡组") },
                    singleLine = true,
                    modifier = Modifier.weight(1f),
                )
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                listOf("all" to "全部", "win" to "胜", "loss" to "负").forEach { (key, label) ->
                    FilterChip(
                        selected = filters.outcome == key,
                        onClick = { onChange(filters.copy(outcome = key)) },
                        label = { Text(label) },
                    )
                }
                Spacer(Modifier.weight(1f))
                TextButton(onClick = onAuth) { Text("令牌") }
            }
            if (filters.from.isNotBlank() || filters.to.isNotBlank()) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "日期 ${filters.from.ifBlank { "不限" }} ~ ${filters.to.ifBlank { "不限" }}",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(onClick = { onChange(filters.copy(from = "", to = "")) }) {
                        Text("清除日期")
                    }
                }
            }
            OutlinedButton(onClick = onSearch, modifier = Modifier.fillMaxWidth()) {
                Text("搜索录像")
            }
        }
    }
}

@Composable
private fun ReplayMatchCard(
    match: ReplayMatch,
    player: String,
    onImport: (ReplayGame) -> Unit,
    onSave: (ReplayGame) -> Unit,
) {
    val result = match.resultFor(player)
    val opponent = match.players.firstOrNull { it != player }
        ?: match.players.firstOrNull().orEmpty()
    val myDeck = match.decks[player].orEmpty()
    val oppDeck = match.decks[opponent].orEmpty()
    Card(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    when (result) {
                        "win" -> "胜"
                        "loss" -> "负"
                        else -> "—"
                    },
                    style = MaterialTheme.typography.titleSmall,
                    color = when (result) {
                        "win" -> Color(0xFF2E7D32)
                        "loss" -> MaterialTheme.colorScheme.error
                        else -> MaterialTheme.colorScheme.outline
                    },
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    opponent.ifBlank { "未知对手" },
                    style = MaterialTheme.typography.titleSmall,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                Text(
                    formatTime(match.start),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
            Text(
                "我方 ${myDeck.ifBlank { "?" }} · 对方 ${oppDeck.ifBlank { "?" }}",
                style = MaterialTheme.typography.bodySmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                "比分 ${match.score.ifBlank { "—" }} · ${match.games.size} 局" +
                    if (!match.available) " · 文件已清理（下载会失败）" else "",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
            Spacer(Modifier.height(4.dp))
            match.games.forEach { game ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "第${game.gameNo}局 · ${sizeText(game.size)}",
                        style = MaterialTheme.typography.labelSmall,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(onClick = { onImport(game) }) { Text("导入 YGOMobile") }
                    TextButton(onClick = { onSave(game) }) { Text("存到下载") }
                }
            }
        }
    }
}

private fun sizeText(bytes: Long): String = when {
    bytes <= 0L -> "—"
    bytes < 1024 -> "${bytes} B"
    bytes < 1024 * 1024 -> "${bytes / 1024} KB"
    else -> String.format("%.1f MB", bytes / 1024.0 / 1024.0)
}

private fun dayKey(seconds: Long): String =
    SimpleDateFormat("yyyy-MM-dd", Locale.US).format(java.util.Date(seconds * 1000))

// ---------- 卡组/录像导出到 YGOMobile ----------

/** API 29+ 走 MediaStore 公共下载目录；旧版本存应用外部目录（免权限）。 */
internal fun saveToDownloads(ctx: Context, fileName: String, bytes: ByteArray): String {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
        val values = android.content.ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, fileName)
            put(MediaStore.Downloads.MIME_TYPE, "application/octet-stream")
        }
        val uri = ctx.contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: throw RuntimeException("无法创建下载记录")
        ctx.contentResolver.openOutputStream(uri)?.use { it.write(bytes) }
            ?: throw RuntimeException("无法写入文件")
        return "下载/$fileName"
    }
    @Suppress("DEPRECATION")
    val dir = File(ctx.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS), "replays")
    if (!dir.exists()) dir.mkdirs()
    val f = File(dir, fileName)
    f.writeBytes(bytes)
    return f.absolutePath
}

private fun copyText(ctx: Context, text: String) {
    val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    cm.setPrimaryClip(ClipData.newPlainText("replay", text))
    Toast.makeText(ctx, "已复制", Toast.LENGTH_SHORT).show()
}

private fun openInBrowser(ctx: Context, url: String) {
    try {
        ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
    } catch (e: Exception) {
        Toast.makeText(ctx, "没有可用浏览器：${e.message}", Toast.LENGTH_SHORT).show()
    }
}

private fun fmt(v: Double): String =
    if (v == v.toLong().toDouble()) v.toLong().toString() else String.format("%.1f", v)

/** 秒 / 毫秒两种时间戳都兼容。 */
private val timeFormat = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault())

fun formatTime(ts: Long): String {
    val ms = if (ts > 1_000_000_000_000L) ts else ts * 1000L
    return if (ts <= 0) "—" else timeFormat.format(Date(ms))
}
