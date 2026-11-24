package com.moecard.watch.ui.tools

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.moecard.watch.tools.SmallWorld
import com.moecard.watch.tools.SwDim
import com.moecard.watch.tools.SwMonster
import com.moecard.watch.tools.SwRoute
import com.moecard.watch.tools.ToolDeckStore
import com.moecard.watch.ui.query.CardImage
import com.moecard.watch.ui.query.EmptyView

/**
 * 小世界计算器：
 *  - 「找路线」：选手牌和想拿的卡，列出卡组里所有能当跳板的怪兽；
 *  - 「能拿到什么」：只选手牌，列出卡组里所有能检索到的怪兽及跳板。
 */
@Composable
fun SmallWorldScreen() {
    val deck by ToolDeckStore.deck.collectAsStateWithLifecycle()
    val vm: ToolCardsViewModel = viewModel(key = "tool-cards")
    val state by vm.state.collectAsStateWithLifecycle()
    LaunchedEffect(deck) { deck?.let { vm.load(it) } }

    var mode by rememberSaveable { mutableStateOf(0) }
    var questionAsZero by rememberSaveable { mutableStateOf(false) }
    var handCode by remember(deck) { mutableStateOf<Long?>(null) }
    var targetCode by remember(deck) { mutableStateOf<Long?>(null) }
    var picking by remember { mutableStateOf<String?>(null) } // "hand" / "target"

    val ready = state as? ToolCardsViewModel.State.Ready
    val monsters = remember(ready) {
        ready?.let { SmallWorld.deckMonsters(it.deck.main, it.infos) }.orEmpty().sortedBy { it.name }
    }
    val hand = monsters.firstOrNull { it.code == handCode }
    val target = monsters.firstOrNull { it.code == targetCode }

    LazyColumn(Modifier.fillMaxSize()) {
        item(key = "source") {
            DeckSourceCard(deck, state, onRetry = { deck?.let { vm.load(it, force = true) } })
        }
        if (ready != null && monsters.isEmpty()) {
            item(key = "empty") { EmptyView("主卡组里没有查到怪兽，小世界只能在怪兽之间用") }
        }
        if (monsters.isNotEmpty()) {
            item(key = "controls") {
                Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp)) {
                    TabRow(selectedTabIndex = mode) {
                        Tab(selected = mode == 0, onClick = { mode = 0 }, text = { Text("找路线") })
                        Tab(selected = mode == 1, onClick = { mode = 1 }, text = { Text("能拿到什么") })
                    }
                    Spacer(Modifier.height(8.dp))
                    PickRow("手牌（展示）", hand) { picking = "hand" }
                    if (mode == 0) {
                        Spacer(Modifier.height(6.dp))
                        PickRow("想加入手牌的卡", target) { picking = "target" }
                    }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "攻守为「?」的怪兽按 0 计算",
                            style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.weight(1f),
                        )
                        Switch(checked = questionAsZero, onCheckedChange = { questionAsZero = it })
                    }
                    Text(
                        if (questionAsZero) "已按 0 处理「?」：请以官方裁定为准。" else
                            "默认不计算攻守含「?」的怪兽：无法判定的组合不给结论。",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                    )
                    Spacer(Modifier.height(8.dp))
                }
            }
            if (mode == 0 && hand != null && target != null) {
                val routes = SmallWorld.routes(hand, target, monsters, questionAsZero)
                item(key = "route-head") {
                    ResultHead(
                        if (routes.isEmpty()) "没有可用的跳板" else "共 ${routes.size} 只跳板",
                        if (routes.isEmpty()) "可以换一张手牌，或在「能拿到什么」里看看这张手牌能拿到哪些卡" else null,
                    )
                }
                items(routes, key = { "r-" + it.bridge.code }) { RouteCard(it) }
            }
            if (mode == 1 && hand != null) {
                val reach = SmallWorld.reachable(hand, monsters, questionAsZero)
                item(key = "reach-head") {
                    ResultHead(
                        if (reach.isEmpty()) "这张手牌拿不到卡组里的任何怪兽" else "能检索到 ${reach.size} 种怪兽",
                        null,
                    )
                }
                items(reach, key = { "t-" + it.first.code }) { (t, routes) -> ReachCard(t, routes) }
            }
        }
        item(key = "pad") { Spacer(Modifier.height(24.dp)) }
    }

    picking?.let { which ->
        MonsterPickerDialog(
            title = if (which == "hand") "选择手牌" else "选择想加入手牌的卡",
            monsters = monsters,
            onPick = {
                if (which == "hand") handCode = it.code else targetCode = it.code
                picking = null
            },
            onDismiss = { picking = null },
        )
    }
}

@Composable
private fun PickRow(label: String, monster: SwMonster?, onClick: () -> Unit) {
    OutlinedCard(Modifier.fillMaxWidth().clickable(onClick = onClick)) {
        Row(Modifier.fillMaxWidth().padding(10.dp), verticalAlignment = Alignment.CenterVertically) {
            if (monster != null) {
                CardImage(monster.code, Modifier.width(36.dp).aspectRatio(59f / 86f))
                Spacer(Modifier.width(10.dp))
            }
            Column(Modifier.weight(1f)) {
                Text(label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline)
                Text(
                    monster?.name ?: "点此选择",
                    style = MaterialTheme.typography.bodyLarge,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                if (monster != null) {
                    Text(statLine(monster), style = MaterialTheme.typography.labelSmall)
                }
            }
        }
    }
}

@Composable
private fun ResultHead(title: String, hint: String?) {
    Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp)) {
        Text(title, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
        if (hint != null) {
            Text(hint, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline)
        }
    }
}

@Composable
private fun RouteCard(route: SwRoute) {
    Card(
        Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Row(Modifier.fillMaxWidth().padding(10.dp), verticalAlignment = Alignment.CenterVertically) {
            CardImage(route.bridge.code, Modifier.width(44.dp).aspectRatio(59f / 86f))
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text("展示卡组里的「${route.bridge.name}」", style = MaterialTheme.typography.bodyMedium)
                Text(statLine(route.bridge), style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline)
                Spacer(Modifier.height(4.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    DimChip("前半步", route.first, route.hand)
                    DimChip("后半步", route.second, route.target)
                }
            }
        }
    }
}

@Composable
private fun ReachCard(target: SwMonster, routes: List<SwRoute>) {
    Card(
        Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Row(Modifier.fillMaxWidth().padding(10.dp)) {
            CardImage(target.code, Modifier.width(44.dp).aspectRatio(59f / 86f))
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text(target.name, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.Bold)
                Text(statLine(target), style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline)
                Spacer(Modifier.height(4.dp))
                routes.forEach { r ->
                    Text(
                        "经「${r.bridge.name}」· ${r.first.label}相同 → ${r.second.label}相同",
                        style = MaterialTheme.typography.labelSmall,
                    )
                }
            }
        }
    }
}

@Composable
private fun DimChip(step: String, dim: SwDim, owner: SwMonster) {
    Surface(color = MaterialTheme.colorScheme.secondaryContainer, shape = MaterialTheme.shapes.small) {
        Text(
            "$step：${dim.label} ${owner.valueText(dim)}",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSecondaryContainer,
            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
        )
    }
}

@Composable
private fun MonsterPickerDialog(
    title: String,
    monsters: List<SwMonster>,
    onPick: (SwMonster) -> Unit,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            LazyColumn(Modifier.heightIn(max = 420.dp)) {
                items(monsters, key = { it.code }) { m ->
                    Row(
                        Modifier.fillMaxWidth().clickable { onPick(m) }.padding(vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        CardImage(m.code, Modifier.width(32.dp).aspectRatio(59f / 86f))
                        Spacer(Modifier.width(8.dp))
                        Column(Modifier.weight(1f)) {
                            Text(
                                m.name + if (m.copies > 1) " ×${m.copies}" else "",
                                style = MaterialTheme.typography.bodyMedium,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                            Text(statLine(m), style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.outline)
                        }
                    }
                    HorizontalDivider()
                }
            }
        },
        confirmButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

private fun statLine(m: SwMonster): String =
    "${m.valueText(SwDim.ATTRIBUTE)} · ${m.valueText(SwDim.RACE)} · ${m.level}星 · " +
        "${m.valueText(SwDim.ATK)}/${m.valueText(SwDim.DEF)}"
