package com.moecard.watch.ui.query

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AssistChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.moecard.watch.query.model.DeckThumbs
import com.moecard.watch.query.model.SideCard
import com.moecard.watch.query.model.SideOpponent
import com.moecard.watch.query.model.SidePlan
import com.moecard.watch.query.model.SideSession
import kotlinx.coroutines.launch

/**
 * 换备页签：当前构筑对「最常遇到的对手卡组」，每个对手取最近 N 场，
 * 先攻 / 后攻的换入换出（口径与 QQ「换备」一致，数据来自服务端
 * /query-api/player/{name}/side-plan，默认只看对手前 200）。
 *
 * 2.2.0 排版：每张卡一行，「卡名 | 先攻 | 后攻」三列对齐，+N 绿色 / −N 红色；
 * 每次都换的实色显示，偶尔换的变淡并标出 局数/总局数。
 */
@Composable
fun SidePlanTab(vm: PlayerDetailViewModel, player: String) {
    val state by vm.sidePlan.collectAsStateWithLifecycle()
    when (val s = state) {
        PlayerDetailViewModel.QueryState.Idle,
        PlayerDetailViewModel.QueryState.Loading -> LoadingView("换备统计中，约几秒…")

        is PlayerDetailViewModel.QueryState.Failed -> Column(Modifier.fillMaxSize().padding(16.dp)) {
            ErrorView(s.message) { vm.retrySidePlan(player) }
            Text(
                "换备只统计当前构筑（主卡相同）的场次，需要该玩家最近 14 天有可用录像。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }

        is PlayerDetailViewModel.QueryState.Ready -> {
            val plan = s.data
            if (plan == null || plan.opponents.isEmpty()) {
                EmptyView("最近 14 天没有可用于换备统计的场次")
            } else {
                SidePlanContent(plan)
            }
        }
    }
}

private val SideIn = Color(0xFF2E7D32)
private val SideOut = Color(0xFFC62828)
private val CellWidth = 60.dp

// ---------- 纯数据整理（单测见 SidePlanRowsTest） ----------

/** 同一张卡在先攻 / 后攻两侧的变化合成一行。 */
internal data class SideRow(val code: Long, val name: String, val first: SideCard?, val second: SideCard?)

/** 两侧都有的排前面，再按总局数从多到少。 */
internal fun mergeSideRows(first: List<SideCard>, second: List<SideCard>): List<SideRow> {
    val map = LinkedHashMap<Long, SideRow>()
    first.forEach { map[it.code] = SideRow(it.code, it.name, it, null) }
    second.forEach { c ->
        map[c.code] = map[c.code]?.copy(second = c) ?: SideRow(c.code, c.name, null, c)
    }
    return map.values.sortedWith(
        compareByDescending<SideRow> { (if (it.first != null) 1 else 0) + (if (it.second != null) 1 else 0) }
            .thenByDescending { (it.first?.sideGames ?: 0) + (it.second?.sideGames ?: 0) },
    )
}

/** 「通用换备」里的一张卡：在 [decks] 个对手卡组面前都会换。 */
internal data class CommonSideCard(val code: Long, val name: String, val decks: Int)

/**
 * 在多数对手卡组面前都换的卡：对手卡组 ≤2 个时要求全部都换，否则要求过半（至少 2 个）。
 */
internal fun commonSideCards(opponents: List<SideOpponent>, added: Boolean): List<CommonSideCard> {
    if (opponents.size < 2) return emptyList()
    val count = LinkedHashMap<Long, Pair<String, Int>>()
    for (op in opponents) {
        val cards = if (added) op.first.added + op.second.added else op.first.removed + op.second.removed
        cards.distinctBy { it.code }.forEach { c ->
            count[c.code] = c.name to ((count[c.code]?.second ?: 0) + 1)
        }
    }
    val threshold = if (opponents.size <= 2) opponents.size else maxOf(2, (opponents.size + 1) / 2)
    return count.filter { it.value.second >= threshold }
        .map { CommonSideCard(it.key, it.value.first, it.value.second) }
        .sortedByDescending { it.decks }
}

private fun copiesText(c: SideCard): String =
    if (c.minCopies == c.maxCopies) "${c.minCopies}" else "${c.minCopies}~${c.maxCopies}"

// ---------- 页面 ----------

@Composable
private fun SidePlanContent(plan: SidePlan) {
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()
    val commonIn = remember(plan) { commonSideCards(plan.opponents, added = true) }
    val commonOut = remember(plan) { commonSideCards(plan.opponents, added = false) }
    val hasCommon = commonIn.isNotEmpty() || commonOut.isNotEmpty()
    val hasJump = plan.opponents.size > 1
    // LazyColumn 里对手卡组之前有几个条目：概览、通用换备（可选）、跳转标签（可选）
    val opponentStart = 1 + (if (hasCommon) 1 else 0) + (if (hasJump) 1 else 0)

    LazyColumn(
        state = listState,
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        item(key = "summary") { SummaryCard(plan) }
        if (hasCommon) {
            item(key = "common") { CommonCard(plan.opponents.size, commonIn, commonOut) }
        }
        if (hasJump) {
            item(key = "jump") {
                Row(
                    Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    plan.opponents.forEachIndexed { i, op ->
                        AssistChip(
                            onClick = { scope.launch { listState.animateScrollToItem(opponentStart + i) } },
                            label = {
                                Text(
                                    "${i + 1}. ${op.deck}",
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                    modifier = Modifier.widthIn(max = 160.dp),
                                )
                            },
                        )
                    }
                }
            }
        }
        itemsIndexed(plan.opponents, key = { i, op -> "op-$i-${op.deck}" }) { i, op ->
            OpponentCard(i + 1, op)
        }
        if (plan.notes.isNotEmpty()) {
            item(key = "notes") {
                OutlinedCard(Modifier.fillMaxWidth()) {
                    Column(Modifier.fillMaxWidth().padding(12.dp)) {
                        Text("口径说明", style = MaterialTheme.typography.titleSmall)
                        Spacer(Modifier.height(4.dp))
                        plan.notes.forEach { note ->
                            Text(
                                "· $note",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.outline,
                            )
                        }
                    }
                }
            }
        }
        item(key = "bottom") { Spacer(Modifier.height(16.dp)) }
    }
}

@Composable
private fun SummaryCard(plan: SidePlan) {
    OutlinedCard(Modifier.fillMaxWidth()) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Text(
                plan.deck ?: "当前构筑",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
            Spacer(Modifier.height(2.dp))
            Text(
                "${plan.totalMatches} 场 · 对手卡组 ${plan.opponents.size} 个" +
                    (plan.opponentRange?.let { " · 只看对手前 $it" } ?: " · 不限对手名次"),
                style = MaterialTheme.typography.bodySmall,
            )
            if (plan.excludedMatches > 0 || plan.unverifiedMatches > 0) {
                Text(
                    "另有 ${plan.excludedMatches} 场对手名次在范围外、${plan.unverifiedMatches} 场名次未核验，未计入",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
        }
    }
}

@Composable
private fun CommonCard(opponents: Int, added: List<CommonSideCard>, removed: List<CommonSideCard>) {
    OutlinedCard(Modifier.fillMaxWidth()) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Text("通用换备", style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
            Text(
                "在多数对手卡组面前都会换的卡（共 $opponents 个对手卡组）",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
            Spacer(Modifier.height(6.dp))
            added.forEach { CommonLine(it, opponents, plus = true) }
            removed.forEach { CommonLine(it, opponents, plus = false) }
        }
    }
}

@Composable
private fun CommonLine(card: CommonSideCard, opponents: Int, plus: Boolean) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp), verticalAlignment = Alignment.CenterVertically) {
        Thumb(card.code)
        Spacer(Modifier.width(6.dp))
        Text(
            card.name,
            style = MaterialTheme.typography.bodySmall,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        Text(
            (if (plus) "换入" else "换出") + " · ${card.decks}/$opponents 个卡组",
            style = MaterialTheme.typography.labelSmall,
            color = if (plus) SideIn else SideOut,
        )
    }
}

@Composable
private fun OpponentCard(index: Int, op: SideOpponent) {
    var expanded by remember(op.deck) { mutableStateOf(false) }
    val ctx = LocalContext.current
    val thumb = remember(op.deck) { DeckThumbs.codeFor(ctx, op.deck) }
    val added = remember(op) { mergeSideRows(op.first.added, op.second.added) }
    val removed = remember(op) { mergeSideRows(op.first.removed, op.second.removed) }

    OutlinedCard(Modifier.fillMaxWidth()) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (thumb != null) {
                    CardImage(
                        thumb,
                        Modifier.size(width = 32.dp, height = 46.dp).clip(RoundedCornerShape(4.dp)),
                    )
                    Spacer(Modifier.width(10.dp))
                }
                Column(Modifier.weight(1f)) {
                    Text(
                        "$index. ${op.deck}",
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.Bold,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Text(
                        buildString {
                            append("${op.recentMatches} 场 · 换边 ${op.sideGames} 局")
                            if (op.matches > op.recentMatches) append("（共遇到 ${op.matches} 场）")
                            if (op.seatUnknownGames > 0) append(" · ${op.seatUnknownGames} 局座位未知未计入")
                        },
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                    )
                }
                if (op.sampleLow) {
                    Surface(color = MaterialTheme.colorScheme.errorContainer, shape = MaterialTheme.shapes.small) {
                        Text(
                            "样本少",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
                        )
                    }
                }
            }
            Spacer(Modifier.height(10.dp))

            // 列头：卡片 | 先攻 · N 局 | 后攻 · N 局
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text(
                    "卡片",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                    modifier = Modifier.weight(1f),
                )
                SeatHead("先攻", op.first.sideGames)
                SeatHead("后攻", op.second.sideGames)
            }
            HorizontalDivider(Modifier.padding(vertical = 4.dp))

            if (added.isEmpty() && removed.isEmpty()) {
                Text(
                    "这个对手没有换备记录",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
            if (added.isNotEmpty()) {
                GroupLabel("换入", SideIn)
                added.forEach { SideRowLine(it, op.first.sideGames, op.second.sideGames, plus = true) }
            }
            if (removed.isNotEmpty()) {
                if (added.isNotEmpty()) Spacer(Modifier.height(4.dp))
                GroupLabel("换出", SideOut)
                removed.forEach { SideRowLine(it, op.first.sideGames, op.second.sideGames, plus = false) }
            }

            if (op.sessions.isNotEmpty()) {
                HorizontalDivider(Modifier.padding(top = 8.dp))
                TextButton(
                    onClick = { expanded = !expanded },
                    contentPadding = PaddingValues(horizontal = 0.dp),
                ) {
                    Text(if (expanded) "收起每场明细" else "每场明细（${op.sessions.size} 场）")
                }
                if (expanded) {
                    op.sessions.forEach { SessionBlock(it) }
                }
            }
        }
    }
}

@Composable
private fun SeatHead(title: String, games: Int) {
    Text(
        "$title\n$games 局",
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.primary,
        textAlign = TextAlign.Center,
        modifier = Modifier.width(CellWidth),
    )
}

@Composable
private fun GroupLabel(text: String, color: Color) {
    Text(
        text,
        style = MaterialTheme.typography.labelMedium,
        fontWeight = FontWeight.Bold,
        color = color,
        modifier = Modifier.padding(vertical = 2.dp),
    )
}

@Composable
private fun Thumb(code: Long) {
    CardImage(code, Modifier.size(width = 22.dp, height = 32.dp).clip(RoundedCornerShape(2.dp)))
}

@Composable
private fun SideRowLine(row: SideRow, firstGames: Int, secondGames: Int, plus: Boolean) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 2.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Thumb(row.code)
        Spacer(Modifier.width(6.dp))
        Text(
            row.name,
            style = MaterialTheme.typography.bodySmall,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        SeatCell(row.first, firstGames, plus)
        SeatCell(row.second, secondGames, plus)
    }
}

/** 一侧的变化：每局都换 → 实色粗体；偶尔换 → 变淡并标出 局数/总局数；没换 → 「—」。 */
@Composable
private fun SeatCell(card: SideCard?, seatGames: Int, plus: Boolean) {
    Column(Modifier.width(CellWidth), horizontalAlignment = Alignment.CenterHorizontally) {
        if (card == null) {
            Text("—", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outlineVariant)
        } else {
            val always = seatGames > 0 && card.sideGames >= seatGames
            val color = if (plus) SideIn else SideOut
            Text(
                (if (plus) "+" else "−") + copiesText(card),
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = if (always) FontWeight.Bold else FontWeight.Normal,
                color = if (always) color else color.copy(alpha = 0.55f),
            )
            if (!always) {
                Text(
                    "${card.sideGames}/$seatGames 局",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun SessionBlock(session: SideSession) {
    val time = session.recordedAt.takeIf { it > 0 }?.let { formatTime(it.toLong()) }
    Column(
        Modifier
            .fillMaxWidth()
            .padding(vertical = 3.dp)
            .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f), RoundedCornerShape(8.dp))
            .padding(8.dp),
    ) {
        Text(
            listOfNotNull(session.opponent ?: "对手未知", time, "换 ${session.sideGames} 局").joinToString(" · "),
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.Bold,
        )
        if (session.added.isEmpty() && session.removed.isEmpty()) {
            Text("这场没有换备", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline)
        }
        if (session.added.isNotEmpty()) {
            Spacer(Modifier.height(4.dp))
            FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                session.added.forEach { DiffChip("+" + chipText(it), SideIn) }
            }
        }
        if (session.removed.isNotEmpty()) {
            Spacer(Modifier.height(4.dp))
            FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                session.removed.forEach { DiffChip("−" + chipText(it), SideOut) }
            }
        }
    }
}

private fun chipText(c: SideCard): String =
    if (c.maxCopies > 1) "${c.name} ×${copiesText(c)}" else c.name

@Composable
private fun DiffChip(text: String, color: Color) {
    Surface(color = color.copy(alpha = 0.12f), shape = RoundedCornerShape(6.dp)) {
        Text(
            text,
            style = MaterialTheme.typography.labelSmall,
            color = color,
            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
        )
    }
}
