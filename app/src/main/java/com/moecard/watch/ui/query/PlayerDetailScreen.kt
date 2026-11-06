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
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.moecard.watch.net.AuthException
import com.moecard.watch.query.model.ArenaMatch
import com.moecard.watch.query.model.ArenaProfile
import com.moecard.watch.query.model.BuildData
import com.moecard.watch.query.model.ReplayData
import com.moecard.watch.query.model.dpCurve
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
class PlayerDetailViewModel : ViewModel() {
    private val api = MoeCardApi()

    val official = MutableStateFlow<OfficialState>(OfficialState.Loading)
    val decks = MutableStateFlow<QueryState<BuildData?>>(QueryState.Idle)
    val replays = MutableStateFlow<QueryState<ReplayData?>>(QueryState.Idle)
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
        replays.value = QueryState.Idle
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

    /** 懒加载：进入录像页签时才拉。 */
    fun loadReplays(player: String) {
        if (replays.value !is QueryState.Idle) return
        replays.value = QueryState.Loading
        viewModelScope.launch {
            try {
                val data = withContext(Dispatchers.IO) { QuerySession.api().replays(player) }
                replays.value = QueryState.Ready(data)
            } catch (e: AuthException) {
                QuerySession.expire()
            } catch (e: Exception) {
                replays.value = QueryState.Failed(e.message ?: "录像加载失败")
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
                    val merged = cur.matches + more.filter {
                        seen.add(it.startTime * 1000_003L + it.endTime)
                    }
                    merged.sortedBy { it.startTime }
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
        TabRow(selectedTabIndex = tab) {
            listOf("概览", "DP曲线", "对战历史", "构筑", "录像").forEachIndexed { i, label ->
                Tab(selected = tab == i, onClick = { tab = i }, text = { Text(label) })
            }
        }
        when (tab) {
            0 -> OverviewTab(official)
            1 -> DpChartTab(official)
            2 -> HistoryTab(vm, player, official)
            3 -> {
                LaunchedEffect(player) { vm.loadDecks(player) }
                DecksTab(vm)
            }
            else -> {
                LaunchedEffect(player) { vm.loadReplays(player) }
                ReplaysTab(vm)
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
                        Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)) {
                            MatchRow(matches[i])
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
private fun DecksTab(vm: PlayerDetailViewModel) {
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
                DeckCardsList(data)
            }
        }
    }
}

/** 构筑卡片列表（主/额外/副卡组分组），玩家详情与卡组详情共用。 */
@Composable
fun DeckCardsList(data: BuildData) {
    val scroll = rememberScrollState()
    Column(Modifier.fillMaxSize().verticalScroll(scroll).padding(16.dp)) {
        Row {
            CompletenessChip("主卡组", data.mainComplete)
            CompletenessChip("额外", data.extraComplete)
            CompletenessChip("副卡组", data.sideComplete)
        }
        Spacer(Modifier.height(8.dp))
        val byPartition = data.cards.groupBy { it.partition }
        listOf(
            "main" to "主卡组",
            "extra" to "额外卡组",
            "side" to "副卡组",
        ).forEach { (key, label) ->
            val cards = byPartition[key] ?: return@forEach
            if (cards.isEmpty()) return@forEach
            Card(
                modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                colors = CardDefaults.outlinedCardColors(),
            ) {
                Column(Modifier.fillMaxWidth().padding(12.dp)) {
                    Text(
                        "$label · ${cards.sumOf { it.copies }} 张",
                        style = MaterialTheme.typography.titleSmall,
                    )
                    Spacer(Modifier.height(4.dp))
                    cards.sortedByDescending { it.copies }.forEach { c ->
                        Text(
                            "${c.copies}× ${c.code}",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
    }
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
private fun ReplaysTab(vm: PlayerDetailViewModel) {
    val state by vm.replays.collectAsStateWithLifecycle()
    val ctx = LocalContext.current
    when (val s = state) {
        PlayerDetailViewModel.QueryState.Idle -> LoadingView()
        PlayerDetailViewModel.QueryState.Loading -> LoadingView("正在拉取录像列表…")
        is PlayerDetailViewModel.QueryState.Failed -> Column(
            Modifier.fillMaxSize().padding(16.dp)
        ) {
            ErrorView(s.message)
            Text(
                "录像仅返回 14 天内现存文件（最多 1000 条），需要查询服务器已启用。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
        is PlayerDetailViewModel.QueryState.Ready -> {
            val data = s.data
            if (data == null || data.items.isEmpty()) {
                EmptyView("14 天内暂无录像")
            } else {
                LazyColumn(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
                    data.link?.let { link ->
                        item(key = "dir-link") {
                            Card(
                                modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp),
                                colors = CardDefaults.outlinedCardColors(),
                            ) {
                                Column(Modifier.fillMaxWidth().padding(12.dp)) {
                                    Text("录像目录", style = MaterialTheme.typography.titleSmall)
                                    Text(
                                        "到期 ${link.expiresAt?.let { formatTime(it) } ?: "—"}" +
                                            (link.isPublic?.let { if (it) " · 公开" else "" } ?: ""),
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.outline,
                                    )
                                    Row {
                                        TextButton(onClick = { copyText(ctx, link.url) }) { Text("复制目录链接") }
                                        TextButton(onClick = { openInBrowser(ctx, link.url) }) { Text("浏览器打开") }
                                    }
                                }
                            }
                        }
                    }
                    items(data.items, key = { it.id + it.url }) { item ->
                        Card(
                            modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                            colors = CardDefaults.outlinedCardColors(),
                        ) {
                            Column(Modifier.fillMaxWidth().padding(12.dp)) {
                                Text(item.name, style = MaterialTheme.typography.bodyMedium)
                                Text(
                                    "录制于 ${formatTime(item.recordedAt.toLong())}",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.outline,
                                )
                                Row {
                                    TextButton(onClick = { downloadReplay(ctx, vm, item.name, item.url) }) {
                                        Text("下载")
                                    }
                                    TextButton(onClick = { copyText(ctx, item.url) }) { Text("复制链接") }
                                    TextButton(onClick = { openInBrowser(ctx, item.url) }) { Text("浏览器") }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

// ---------- 录像下载 / 工具 ----------

private fun downloadReplay(ctx: Context, vm: PlayerDetailViewModel, name: String, url: String) {
    Toast.makeText(ctx, "开始下载…", Toast.LENGTH_SHORT).show()
    vm.viewModelScope.launch(Dispatchers.IO) {
        try {
            val bytes = QuerySession.api().downloadBytes(url)
            val fileName = name.ifBlank { "replay.yrp" }.let { if (it.endsWith(".yrp")) it else "$it.yrp" }
            val saved = saveToDownloads(ctx, fileName, bytes)
            withContext(Dispatchers.Main) {
                Toast.makeText(ctx, "已保存：$saved", Toast.LENGTH_LONG).show()
            }
        } catch (e: Exception) {
            withContext(Dispatchers.Main) {
                Toast.makeText(ctx, "下载失败：${e.message}", Toast.LENGTH_LONG).show()
            }
        }
    }
}

/** API 29+ 走 MediaStore 公共下载目录；旧版本存应用外部目录（免权限）。 */
private fun saveToDownloads(ctx: Context, fileName: String, bytes: ByteArray): String {
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
