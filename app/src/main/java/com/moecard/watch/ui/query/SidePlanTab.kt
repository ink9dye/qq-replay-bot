package com.moecard.watch.ui.query

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.moecard.watch.query.model.SideCard
import com.moecard.watch.query.model.SideOpponent
import com.moecard.watch.query.model.SidePlan
import com.moecard.watch.query.model.SideSeat

/**
 * 换备页签：当前构筑对「最常遇到的对手卡组」，每个对手取最近 N 场，
 * 先攻 / 后攻分列显示换入换出（口径与 QQ「换备」一致，数据来自服务端
 * /query-api/player/{name}/side-plan，默认只看对手前 200）。
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

@Composable
private fun SidePlanContent(plan: SidePlan) {
    val scroll = rememberScrollState()
    Column(Modifier.fillMaxSize().verticalScroll(scroll).padding(16.dp)) {
        Card(Modifier.fillMaxWidth(), colors = CardDefaults.outlinedCardColors()) {
            Column(Modifier.fillMaxWidth().padding(12.dp)) {
                Text(
                    plan.deck ?: "当前构筑",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.Bold,
                )
                Spacer(Modifier.height(4.dp))
                Text(
                    "共 ${plan.totalMatches} 场 · 对手卡组 ${plan.opponents.size} 个" +
                        (plan.opponentRange?.let { " · 只看对手前 $it" } ?: " · 不限对手名次" ),
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
        Spacer(Modifier.height(8.dp))
        plan.opponents.forEachIndexed { index, opponent ->
            OpponentCard(index + 1, opponent)
            Spacer(Modifier.height(8.dp))
        }
        if (plan.notes.isNotEmpty()) {
            Card(Modifier.fillMaxWidth(), colors = CardDefaults.outlinedCardColors()) {
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
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun OpponentCard(index: Int, opponent: SideOpponent) {
    var expanded by remember(opponent.deck) { mutableStateOf(false) }
    Card(Modifier.fillMaxWidth(), colors = CardDefaults.outlinedCardColors()) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    "$index. ${opponent.deck}",
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.weight(1f),
                )
                if (opponent.sampleLow) {
                    Text(
                        "样本少",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            }
            Text(
                "遇到 ${opponent.matches} 场 · 取最近 ${opponent.recentMatches} 场 · 换边局 ${opponent.sideGames} 个" +
                    if (opponent.seatUnknownGames > 0) "（${opponent.seatUnknownGames} 局座位未知未计入）" else "",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
            Spacer(Modifier.height(8.dp))
            Row(Modifier.fillMaxWidth()) {
                SeatColumn("先攻", opponent.first, Modifier.weight(1f))
                Spacer(Modifier.padding(horizontal = 6.dp))
                SeatColumn("后攻", opponent.second, Modifier.weight(1f))
            }
            if (opponent.sessions.isNotEmpty()) {
                TextButton(onClick = { expanded = !expanded }) {
                    Text(if (expanded) "收起每场明细" else "查看每场明细（${opponent.sessions.size} 场）")
                }
                if (expanded) {
                    opponent.sessions.forEach { session ->
                        val when_ = session.recordedAt.takeIf { it > 0 }?.let { formatTime(it.toLong()) }
                        Text(
                            buildString {
                                append("· ")
                                append(session.opponent ?: "（对手未知）")
                                if (when_ != null) append("  $when_")
                                append("  换 ${session.sideGames} 局")
                                if (session.added.isNotEmpty()) append("  入: " + session.added.joinToString("、") { it.name })
                                if (session.removed.isNotEmpty()) append("  出: " + session.removed.joinToString("、") { it.name })
                            },
                            style = MaterialTheme.typography.labelSmall,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun SeatColumn(title: String, seat: SideSeat, modifier: Modifier = Modifier) {
    Column(modifier) {
        Text(
            "$title · ${seat.sideGames} 局",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.primary,
        )
        Spacer(Modifier.height(2.dp))
        if (seat.added.isEmpty() && seat.removed.isEmpty()) {
            Text(
                "无换备记录",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
            return@Column
        }
        if (seat.added.isNotEmpty()) {
            Text("换入", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.primary)
            seat.added.forEach { SideCardLine(it) }
        }
        if (seat.removed.isNotEmpty()) {
            Text("换出", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.error)
            seat.removed.forEach { SideCardLine(it) }
        }
    }
}

@Composable
private fun SideCardLine(card: SideCard) {
    Row(Modifier.fillMaxWidth().padding(vertical = 1.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(
            card.name,
            style = MaterialTheme.typography.bodySmall,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        Text(
            "${card.copiesLabel} · ${card.sessions}场/${card.sideGames}局",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
        )
    }
}
