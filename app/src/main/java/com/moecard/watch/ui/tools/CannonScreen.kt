package com.moecard.watch.ui.tools

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.moecard.watch.tools.Cannon
import com.moecard.watch.tools.ToolDeckStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

private val ClearColor = Color(0xFF2E7D32)
private val BanishOnlyColor = Color(0xFFF9A825)

/**
 * 联立炮（联栗炮固定式）计算器。
 *
 * A = 双方手牌与场上的卡片总数（通常包括正在发动的这张联栗炮），
 * B = 对方要处理的那只表侧怪兽的等级 / 阶级。
 */
@Composable
fun CannonScreen() {
    val deck by ToolDeckStore.deck.collectAsStateWithLifecycle()
    val vm: ToolCardsViewModel = viewModel(key = "tool-cards")
    val state by vm.state.collectAsStateWithLifecycle()
    LaunchedEffect(deck) { deck?.let { vm.load(it) } }
    val ready = state as? ToolCardsViewModel.State.Ready

    // 额外卡组统计：卡组加载完先按卡片数据算一遍，之后允许手动改
    var extra by remember(ready) {
        mutableStateOf(ready?.let { Cannon.Pool.fromCards(it.deck.extra, it.infos) } ?: Cannon.Pool())
    }
    var banished by remember { mutableStateOf(Cannon.Pool()) }
    var showBanished by rememberSaveable { mutableStateOf(false) }
    var a by rememberSaveable { mutableStateOf(12) }
    var b by rememberSaveable { mutableStateOf(8) }

    val result = remember(a, b, extra, banished) { Cannon.solve(a, b, extra, banished) }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        DeckSourceCard(deck, state, onRetry = { deck?.let { vm.load(it, force = true) } })

        Section("额外卡组") {
            Text(
                if (ready != null) "已按卡组里的超量 / 融合怪兽统计，可以手动修改。" else
                    "没有导入卡组时可以直接手动填写。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
            PoolEditor(extra, onChange = { extra = it })
        }

        Section("除外区（可选）") {
            Text(
                "第 2 步可以用除外区原本就有的超量 / 融合怪兽（比如之前用过的联栗炮）。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
            TextButton(onClick = { showBanished = !showBanished }) {
                Text(if (showBanished) "收起" else if (banished.isEmpty) "填写除外区" else "修改除外区")
            }
            if (showBanished) PoolEditor(banished, onChange = { banished = it })
        }

        Section("场况") {
            NumberStepper("A：双方手牌 + 场上卡片总数", a, 3..40, { a = it }, Modifier.fillMaxWidth())
            Text(
                "通常包括正在发动的这张联栗炮本身。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
            NumberStepper("B：对方怪兽的等级 / 阶级", b, 1..13, { b = it }, Modifier.fillMaxWidth())
        }

        ResultCard(result)

        Section("A × B 一览（点格子带入）") {
            Legend()
            Heatmap(extra, banished, selectedA = a, selectedB = b) { na, nb -> a = na; b = nb }
        }

        RecommendSection(onApply = { extra = it })

        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun Section(title: String, content: @Composable () -> Unit) {
    Card(
        Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Text(title, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(4.dp))
            content()
        }
    }
}

/** 超量（阶级 1–13）与融合（等级 1–12）各一排小步进格。 */
@Composable
private fun PoolEditor(pool: Cannon.Pool, onChange: (Cannon.Pool) -> Unit) {
    Text("超量怪兽（阶级 × 张数）", style = MaterialTheme.typography.labelMedium)
    CountGrid(
        keys = (Cannon.RANK_MIN..Cannon.RANK_MAX).toList(),
        prefix = "",
        suffix = "阶",
        counts = pool.xyz,
        onSet = { k, n -> onChange(pool.withXyz(k, n)) },
    )
    Spacer(Modifier.height(6.dp))
    Text("融合怪兽（等级 × 张数）", style = MaterialTheme.typography.labelMedium)
    CountGrid(
        keys = (Cannon.LEVEL_MIN..Cannon.LEVEL_MAX).toList(),
        prefix = "",
        suffix = "星",
        counts = pool.fusion,
        onSet = { k, n -> onChange(pool.withFusion(k, n)) },
    )
}

@Composable
private fun CountGrid(
    keys: List<Int>,
    prefix: String,
    suffix: String,
    counts: Map<Int, Int>,
    onSet: (Int, Int) -> Unit,
) {
    keys.chunked(4).forEach { row ->
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            row.forEach { k ->
                val n = counts[k] ?: 0
                Surface(
                    modifier = Modifier.weight(1f).padding(vertical = 2.dp),
                    shape = MaterialTheme.shapes.small,
                    color = if (n > 0) MaterialTheme.colorScheme.secondaryContainer else
                        MaterialTheme.colorScheme.surfaceVariant,
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "−",
                            modifier = Modifier.clickable { if (n > 0) onSet(k, n - 1) }.padding(horizontal = 8.dp, vertical = 6.dp),
                        )
                        Text(
                            "$prefix$k$suffix\n×$n",
                            style = MaterialTheme.typography.labelSmall,
                            textAlign = TextAlign.Center,
                            modifier = Modifier.weight(1f),
                        )
                        Text(
                            "+",
                            modifier = Modifier.clickable { if (n < 15) onSet(k, n + 1) }.padding(horizontal = 8.dp, vertical = 6.dp),
                        )
                    }
                }
            }
            repeat(4 - row.size) { Spacer(Modifier.weight(1f)) }
        }
    }
}

@Composable
private fun ResultCard(r: Cannon.Result) {
    val color = when (r.status) {
        Cannon.Status.CLEAR -> ClearColor
        Cannon.Status.BANISH_ONLY -> BanishOnlyColor
        Cannon.Status.NONE -> MaterialTheme.colorScheme.outline
    }
    Section("结果：A = ${r.a}，B = ${r.b}") {
        Text(r.status.label, color = color, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(4.dp))
        if (r.plans.isEmpty()) {
            Text("额外卡组里凑不出 2 × 阶级 + 融合等级 = ${r.a}。", style = MaterialTheme.typography.bodySmall)
        }
        r.plans.forEach { plan ->
            HorizontalDivider(Modifier.padding(vertical = 4.dp))
            Text(
                "第 1 步：除外 2 只 ${plan.step1.rank} 阶超量 + 1 只 ${plan.step1.level} 星融合" +
                    "（2×${plan.step1.rank}+${plan.step1.level}=${r.a}）",
                style = MaterialTheme.typography.bodyMedium,
            )
            if (plan.step2.isEmpty()) {
                Text(
                    "第 2 步：除外区凑不出 阶级 + 等级 = ${r.b}，清不了场",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            } else {
                plan.step2.forEach { s2 ->
                    Text(
                        "第 2 步：${s2.rank} 阶超量 + ${s2.level} 星融合回额外卡组" +
                            if (s2.fromStep1) "（就用刚除外的）" else "（用到除外区原有的卡）",
                        style = MaterialTheme.typography.bodySmall,
                        color = ClearColor,
                    )
                }
            }
        }
        if (r.status != Cannon.Status.CLEAR) {
            Spacer(Modifier.height(6.dp))
            val ideal = r.ideal
            Text(
                if (ideal != null) {
                    "想在这个场况清场：额外卡组放 2 只 ${ideal.rank} 阶超量 + 1 只 ${ideal.level} 星融合" +
                        "（x = A−B = ${ideal.rank}，y = 2B−A = ${ideal.level}）。"
                } else {
                    "只靠同一组卡无解（A−B 或 2B−A 超出阶级 / 等级范围），需要除外区原有的卡配合。"
                },
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
    }
}

@Composable
private fun Legend() {
    Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
        LegendItem(ClearColor, "能清场")
        LegendItem(BanishOnlyColor, "清不了场")
        LegendItem(MaterialTheme.colorScheme.surfaceVariant, "发动不了")
    }
    Spacer(Modifier.height(6.dp))
}

@Composable
private fun LegendItem(color: Color, label: String) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(12.dp).background(color))
        Spacer(Modifier.width(4.dp))
        Text(label, style = MaterialTheme.typography.labelSmall)
    }
}

private val HEAT_A = 5..30
private val HEAT_B = 1..13

@Composable
private fun Heatmap(
    extra: Cannon.Pool,
    banished: Cannon.Pool,
    selectedA: Int,
    selectedB: Int,
    onPick: (Int, Int) -> Unit,
) {
    val grid = remember(extra, banished) { Cannon.heatmap(extra, banished, HEAT_A, HEAT_B) }
    val none = MaterialTheme.colorScheme.surfaceVariant
    val outline = MaterialTheme.colorScheme.onSurface
    Column(Modifier.horizontalScroll(rememberScrollState())) {
        Row {
            Box(Modifier.size(width = 28.dp, height = 18.dp))
            HEAT_B.forEach { b ->
                Text(
                    "$b", fontSize = 9.sp, textAlign = TextAlign.Center,
                    modifier = Modifier.width(20.dp),
                )
            }
        }
        HEAT_A.forEachIndexed { i, a ->
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("A$a", fontSize = 9.sp, modifier = Modifier.width(28.dp))
                HEAT_B.forEachIndexed { j, b ->
                    val status = grid[i][j]
                    val c = when (status) {
                        Cannon.Status.CLEAR -> ClearColor
                        Cannon.Status.BANISH_ONLY -> BanishOnlyColor
                        Cannon.Status.NONE -> none
                    }
                    val selected = a == selectedA && b == selectedB
                    Box(
                        Modifier
                            .padding(1.dp)
                            .size(18.dp)
                            .background(c)
                            .then(if (selected) Modifier.border(2.dp, outline) else Modifier)
                            .clickable { onPick(a, b) },
                    )
                }
            }
        }
    }
    Text(
        "行是 A（$HEAT_A），列是 B（$HEAT_B）。",
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.outline,
    )
}

/** “固定式”配置推荐：给定额外卡组格数，覆盖尽量多的 (A, B)。 */
@Composable
private fun RecommendSection(onApply: (Cannon.Pool) -> Unit) {
    var slots by rememberSaveable { mutableStateOf(6) }
    var aMin by rememberSaveable { mutableStateOf(8) }
    var aMax by rememberSaveable { mutableStateOf(18) }
    var bMin by rememberSaveable { mutableStateOf(1) }
    var bMax by rememberSaveable { mutableStateOf(12) }
    var results by remember { mutableStateOf<List<Cannon.Loadout>?>(null) }
    var busy by remember { mutableStateOf(false) }
    var request by remember { mutableStateOf(0) }

    LaunchedEffect(request) {
        if (request == 0) return@LaunchedEffect
        busy = true
        results = withContext(Dispatchers.Default) {
            Cannon.recommend(slots, aMin..aMax, bMin..bMax)
        }
        busy = false
    }

    Section("固定式配置推荐") {
        Text(
            "只看额外卡组本身（除外区为空）：给定格数，挑出能在最多 (A, B) 场况下清场的超量阶级（每种 2 只）和融合等级（每种 1 只）。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
        )
        NumberStepper("额外卡组格数", slots, 3..15, { slots = it }, Modifier.fillMaxWidth())
        NumberStepper("A 最小", aMin, 3..aMax, { aMin = it }, Modifier.fillMaxWidth())
        NumberStepper("A 最大", aMax, aMin..40, { aMax = it }, Modifier.fillMaxWidth())
        NumberStepper("B 最小", bMin, 1..bMax, { bMin = it }, Modifier.fillMaxWidth())
        NumberStepper("B 最大", bMax, bMin..13, { bMax = it }, Modifier.fillMaxWidth())
        OutlinedButton(onClick = { request += 1 }, enabled = !busy) {
            Text(if (busy) "计算中…" else "计算推荐")
        }
        results?.let { list ->
            if (list.isEmpty()) {
                Text("这个范围内没有可行配置，放宽 A / B 范围或增加格数试试。", style = MaterialTheme.typography.bodySmall)
            }
            list.forEachIndexed { i, lo ->
                HorizontalDivider(Modifier.padding(vertical = 4.dp))
                Text(
                    "方案 ${i + 1}：超量 " + lo.ranks.joinToString("、") { "${it}阶×2" } +
                        " ＋ 融合 " + lo.levels.joinToString("、") { "${it}星" },
                    style = MaterialTheme.typography.bodyMedium,
                )
                val pct = if (lo.total == 0) 0 else lo.covered * 100 / lo.total
                Text(
                    "占 ${lo.slots} 格 · 能清场 ${lo.covered}/${lo.total} 种场况（$pct%）",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                TextButton(onClick = {
                    var pool = Cannon.Pool()
                    lo.ranks.forEach { pool = pool.withXyz(it, 2) }
                    lo.levels.forEach { pool = pool.withFusion(it, 1) }
                    onApply(pool)
                }) { Text("套用到上面的额外卡组") }
            }
        }
    }
}
