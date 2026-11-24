package com.moecard.watch.ui.query

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Info
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.moecard.watch.net.AuthException
import com.moecard.watch.query.model.CardFlow
import com.moecard.watch.query.model.CardUsage
import com.moecard.watch.query.model.CardWinrate
import com.moecard.watch.query.model.CountDist
import com.moecard.watch.query.model.FlowDeck
import com.moecard.watch.query.model.MatchupRow
import com.moecard.watch.query.model.RateBase
import com.moecard.watch.query.model.UsageRecord
import com.moecard.watch.query.net.QuerySession
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.Locale
import kotlin.math.abs

/**
 * 单卡统计：使用量 / 单卡胜率 / 单卡流向（数据来自
 * /query-api/card/{code}/(usage|flow|winrate)，口径为当前前百构筑）。
 *
 * 三个接口串行请求：服务端 heavy 闸门只有 2 路，客户端并发只会自己把自己挤成 429。
 * 每块独立成败，任何一块失败都不影响其他两块和卡片基础信息。
 * 2.2.0：状态提到 [CardStatsState]，卡片详情页顶部的「关键数字」与下面三块共用一份数据。
 */
internal sealed interface Load<out T> {
    object Loading : Load<Nothing>
    data class Ok<T>(val value: T) : Load<T>
    data class Err(val message: String) : Load<Nothing>
}

internal class CardStatsState {
    var usage by mutableStateOf<Load<CardUsage>>(Load.Loading)
    var winrate by mutableStateOf<Load<CardWinrate>>(Load.Loading)
    var flow by mutableStateOf<Load<CardFlow>>(Load.Loading)
    var attempt by mutableStateOf(0)

    fun retry() {
        attempt++
    }
}

@Composable
internal fun rememberCardStats(code: Long): CardStatsState {
    val s = remember(code) { CardStatsState() }
    LaunchedEffect(code, s.attempt) {
        s.usage = Load.Loading
        s.winrate = Load.Loading
        s.flow = Load.Loading
        s.usage = loadIo { QuerySession.api().cardUsage(code) }
        s.winrate = loadIo { QuerySession.api().cardWinrate(code) }
        s.flow = loadIo { QuerySession.api().cardFlow(code) }
    }
    return s
}

private suspend fun <T> loadIo(block: () -> T): Load<T> = withContext(Dispatchers.IO) {
    try {
        Load.Ok(block())
    } catch (e: AuthException) {
        QuerySession.expire()
        Load.Err("登录已过期，请重新登录")
    } catch (e: Exception) {
        Load.Err(e.message ?: "加载失败")
    }
}

private val Up = Color(0xFF2E7D32)
private val Down = Color(0xFFC62828)

private fun pct(n: Int, d: Int): String =
    if (d <= 0) "—" else String.format(Locale.ROOT, "%.0f%%", n * 100.0 / d)

/** 胜率着色：高于 50% 绿、低于 50% 红。 */
@Composable
private fun rateColor(rate: Double?): Color = when {
    rate == null -> MaterialTheme.colorScheme.onSurface
    rate > 50.0 -> Up
    rate < 50.0 -> Down
    else -> MaterialTheme.colorScheme.onSurface
}

/** 带这张卡的人数：主卡组 / Side / 合计（只要分布里 ≥1 张就算）。 */
internal data class Carry(val denom: Int, val total: Int, val main: Int, val side: Int)

internal fun carryOf(u: CardUsage): Carry {
    fun users(d: CountDist) = (d.total - d.countOf(0)).coerceAtLeast(0)
    return Carry(u.totalDist.total, users(u.totalDist), users(u.mainDist), users(u.sideDist))
}

/** 一句话用法：前百没人带 / 多数 Side 换入 / 放在主卡组 / 主卡与 Side 都有。 */
internal fun usageMode(c: Carry): String = when {
    c.total <= 0 -> "前百没人带"
    c.main == 0 -> "全部从 Side 换入"
    c.side == 0 -> "全部放在主卡组"
    c.side > c.main -> "多数从 Side 换入（主卡 ${c.main} 人 · Side ${c.side} 人）"
    else -> "多数放在主卡组（主卡 ${c.main} 人 · Side ${c.side} 人）"
}

/** 名单里真正带了这张卡的人，按张数从多到少、名次从高到低。 */
internal fun carriers(records: List<UsageRecord>): List<UsageRecord> =
    records.filter { it.total > 0 || it.main > 0 || it.side > 0 }
        .sortedWith(
            compareByDescending<UsageRecord> { maxOf(it.total, it.main + it.side) }
                .thenBy { if (it.rank > 0) it.rank else Int.MAX_VALUE },
        )

/** 流向里真正有人带的卡组，按带的人数排序。 */
internal fun carryingDecks(items: List<FlowDeck>): List<FlowDeck> =
    items.filter { it.total.used > 0 }
        .sortedWith(compareByDescending<FlowDeck> { it.total.used }.thenByDescending { it.total.rate ?: 0.0 })

// ---------- 顶部关键数字 ----------

@Composable
internal fun CardKeyNumbers(stats: CardStatsState) {
    val usage = (stats.usage as? Load.Ok)?.value
    val win = (stats.winrate as? Load.Ok)?.value
    val usageLoading = stats.usage is Load.Loading
    val winLoading = stats.winrate is Load.Loading
    Card(
        Modifier.fillMaxWidth().padding(vertical = 6.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.secondaryContainer.copy(alpha = 0.55f),
        ),
    ) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            val carry = usage?.let { carryOf(it) }
            Row(Modifier.fillMaxWidth()) {
                KeyNumber(
                    "携带率",
                    carry?.let { pct(it.total, it.denom) },
                    carry?.let { "${it.total}/${it.denom} 人" },
                    usageLoading,
                    Modifier.weight(1f),
                )
                KeyNumber(
                    "胜率",
                    win?.base?.takeIf { it.games > 0 }?.rateText,
                    win?.base?.takeIf { it.games > 0 }?.let { "${it.games} 局" },
                    winLoading,
                    Modifier.weight(1f),
                    color = rateColor(win?.base?.rate),
                )
            }
            Spacer(Modifier.height(8.dp))
            Row(Modifier.fillMaxWidth()) {
                KeyNumber(
                    "先攻",
                    win?.first?.takeIf { it.games > 0 }?.rateText,
                    win?.first?.takeIf { it.games > 0 }?.let { "${it.games} 局" },
                    winLoading,
                    Modifier.weight(1f),
                    color = rateColor(win?.first?.rate),
                )
                KeyNumber(
                    "后攻",
                    win?.second?.takeIf { it.games > 0 }?.rateText,
                    win?.second?.takeIf { it.games > 0 }?.let { "${it.games} 局" },
                    winLoading,
                    Modifier.weight(1f),
                    color = rateColor(win?.second?.rate),
                )
            }
            if (carry != null) {
                Spacer(Modifier.height(6.dp))
                Text(usageMode(carry), style = MaterialTheme.typography.bodySmall)
            }
            seatInsight(win)?.let {
                Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.primary)
            }
        }
    }
}

/** 先后攻胜率差距明显（两边各 ≥10 局、相差 ≥10 个百分点）时给一句提示。 */
internal fun seatInsight(w: CardWinrate?): String? {
    val f = w?.first ?: return null
    val s = w.second ?: return null
    val fr = f.rate ?: return null
    val sr = s.rate ?: return null
    if (f.games < 10 || s.games < 10) return null
    val diff = fr - sr
    if (abs(diff) < 10.0) return null
    return String.format(
        Locale.ROOT,
        "先攻胜率比后攻%s %.1f 个百分点",
        if (diff > 0) "高" else "低",
        abs(diff),
    )
}

@Composable
private fun KeyNumber(
    label: String,
    value: String?,
    sub: String?,
    loading: Boolean,
    modifier: Modifier = Modifier,
    color: Color = MaterialTheme.colorScheme.onSurface,
) {
    Column(modifier) {
        Text(label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline)
        if (loading) {
            SkeletonLine(0.5f, 22)
            Spacer(Modifier.height(4.dp))
            SkeletonLine(0.35f, 10)
        } else {
            Text(
                value ?: "—",
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.Bold,
                color = if (value == null) MaterialTheme.colorScheme.outline else color,
            )
            Text(
                sub ?: "无数据",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
    }
}

// ---------- 三块明细 ----------

@Composable
internal fun CardStatsSection(stats: CardStatsState) {
    Spacer(Modifier.height(8.dp))
    Text("单卡统计（当前前百）", style = MaterialTheme.typography.titleMedium)
    Spacer(Modifier.height(4.dp))
    UsageBlock(stats.usage) { stats.retry() }
    WinrateBlock(stats.winrate) { stats.retry() }
    FlowBlock(stats.flow) { stats.retry() }
}

@Composable
private fun SectionCard(
    title: String,
    action: @Composable () -> Unit = {},
    content: @Composable () -> Unit,
) {
    Card(Modifier.fillMaxWidth().padding(vertical = 4.dp), colors = CardDefaults.outlinedCardColors()) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    title,
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.weight(1f),
                )
                action()
            }
            Spacer(Modifier.height(6.dp))
            content()
        }
    }
}

@Composable
private fun SkeletonLine(fraction: Float, heightDp: Int = 12) {
    Box(
        Modifier
            .fillMaxWidth(fraction)
            .height(heightDp.dp)
            .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(4.dp)),
    )
}

/** 加载中先画灰色占位，数据到了再替换，页面不会跳来跳去。 */
@Composable
private fun Skeleton() {
    Column {
        listOf(0.9f, 0.7f, 0.8f, 0.5f).forEach {
            SkeletonLine(it)
            Spacer(Modifier.height(8.dp))
        }
    }
}

@Composable
private fun <T> SectionBody(state: Load<T>, retry: () -> Unit, content: @Composable (T) -> Unit) {
    when (state) {
        Load.Loading -> Skeleton()
        is Load.Err -> Column {
            Text(
                state.message,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
            Spacer(Modifier.height(4.dp))
            OutlinedButton(onClick = retry) { Text("重试") }
        }
        is Load.Ok -> content(state.value)
    }
}

@Composable
private fun Muted(text: String) {
    Text(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline)
}

// ---------- 使用量 ----------

@Composable
private fun UsageBlock(state: Load<CardUsage>, retry: () -> Unit) {
    SectionCard("使用量") {
        SectionBody(state, retry) { u ->
            val c = carryOf(u)
            if (c.denom <= 0 || c.total <= 0) {
                Muted("前百构筑里没人带这张卡（已统计 ${u.covered}/${u.current} 人）。")
                return@SectionBody
            }
            Text(
                "${c.total}/${c.denom} 人带（${pct(c.total, c.denom)}）· ${usageMode(c)}",
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Bold,
            )
            if (u.excluded > 0 || u.covered < u.current) {
                Text(
                    "已统计 ${u.covered}/${u.current} 人" +
                        if (u.excluded > 0) "，另 ${u.excluded} 人无确认首局未计入" else "",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
            Spacer(Modifier.height(8.dp))
            CopiesTable(u)
            Spacer(Modifier.height(4.dp))
            Text(
                "总投入 ${u.totalTotal} 张：主卡 ${u.mainTotal} · Side ${u.sideTotal}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )

            val list = carriers(u.records)
            Spacer(Modifier.height(8.dp))
            HorizontalDivider()
            Spacer(Modifier.height(6.dp))
            Text(
                if (u.truncated) "带了的玩家（名单前 ${u.records.size} 人里有 ${list.size} 人）"
                else "带了的玩家（${list.size} 人）",
                style = MaterialTheme.typography.labelMedium,
            )
            if (list.isEmpty()) {
                Muted("名单里没有带这张卡的玩家")
            }
            list.take(8).forEach { r ->
                Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
                    Text(
                        "${if (r.rank > 0) "#${r.rank} " else ""}${r.player}",
                        style = MaterialTheme.typography.bodySmall,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f),
                    )
                    Text(
                        listOfNotNull(
                            r.main.takeIf { it > 0 }?.let { "主卡 $it" },
                            r.side.takeIf { it > 0 }?.let { "Side $it" },
                        ).joinToString(" · ").ifEmpty { "${r.total} 张" },
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                    )
                }
            }
            if (list.size > 8) Muted("…另有 ${list.size - 8} 人")
        }
    }
}

/** 只统计带了这张卡的人：行 = 1/2/3+ 张，列 = 主卡 / Side / 合计。全是 0 的行不画。 */
@Composable
private fun Cell(text: String, bold: Boolean = false, muted: Boolean = false) {
    Text(
        text,
        style = MaterialTheme.typography.bodySmall,
        fontWeight = if (bold) FontWeight.Bold else FontWeight.Normal,
        color = if (muted) MaterialTheme.colorScheme.outline else MaterialTheme.colorScheme.onSurface,
        textAlign = TextAlign.End,
        modifier = Modifier.width(64.dp),
    )
}

@Composable
private fun CopiesTable(u: CardUsage) {
    Row(Modifier.fillMaxWidth()) {
        Text("张数", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline,
            modifier = Modifier.weight(1f))
        Cell("主卡", muted = true)
        Cell("Side", muted = true)
        Cell("合计", muted = true)
    }
    for (n in 1..3) {
        val m = u.mainDist.countOf(n)
        val s = u.sideDist.countOf(n)
        val t = u.totalDist.countOf(n)
        if (m == 0 && s == 0 && t == 0) continue
        Row(Modifier.fillMaxWidth().padding(vertical = 1.dp)) {
            Text(if (n >= 3) "3+ 张" else "$n 张", style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.weight(1f))
            Cell(if (m > 0) "$m 人" else "—", muted = m == 0)
            Cell(if (s > 0) "$s 人" else "—", muted = s == 0)
            Cell(if (t > 0) "$t 人" else "—", bold = t > 0, muted = t == 0)
        }
    }
}

@Composable
private fun RateBar(fraction: Float, modifier: Modifier = Modifier, color: Color = MaterialTheme.colorScheme.primary) {
    Box(
        modifier
            .height(4.dp)
            .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(2.dp)),
    ) {
        Box(
            Modifier
                .fillMaxWidth(fraction.coerceIn(0f, 1f))
                .height(4.dp)
                .background(color, RoundedCornerShape(2.dp)),
        )
    }
}

// ---------- 单卡胜率 ----------

@Composable
private fun WinrateBlock(state: Load<CardWinrate>, retry: () -> Unit) {
    var showNotes by remember { mutableStateOf(false) }
    val notes = (state as? Load.Ok)?.value?.notes.orEmpty()
    SectionCard(
        "单卡胜率",
        action = {
            if (notes.isNotEmpty()) {
                IconButton(onClick = { showNotes = !showNotes }, modifier = Modifier.size(32.dp)) {
                    Icon(Icons.Outlined.Info, contentDescription = "口径说明", modifier = Modifier.size(18.dp))
                }
            }
        },
    ) {
        SectionBody(state, retry) { w ->
            if (showNotes) {
                Column(
                    Modifier
                        .fillMaxWidth()
                        .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f), RoundedCornerShape(6.dp))
                        .padding(8.dp),
                ) {
                    w.notes.forEach {
                        Text("· $it", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline)
                    }
                }
                Spacer(Modifier.height(6.dp))
            }
            val base = w.base
            if (base == null || base.games <= 0) {
                Muted("没有“使用该卡且胜负明确”的小局。")
                return@SectionBody
            }
            Row(Modifier.fillMaxWidth()) {
                RateCell("总体", base, Modifier.weight(1f))
                RateCell("先攻", w.first, Modifier.weight(1f))
                RateCell("后攻", w.second, Modifier.weight(1f))
            }
            seatInsight(w)?.let {
                Spacer(Modifier.height(4.dp))
                Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.primary)
            }
            if (w.rows.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                HorizontalDivider()
                Spacer(Modifier.height(6.dp))
                Text(
                    "对阵卡组（共 ${w.totalDecks} 个，列出 ${w.rows.size} 个）",
                    style = MaterialTheme.typography.labelMedium,
                )
                // 样本少的排到最后并变淡，不再在名字后面加括号
                w.rows.sortedWith(compareBy<MatchupRow> { it.lowSample }.thenByDescending { it.games })
                    .take(10)
                    .forEach { MatchupLine(it) }
            }
        }
    }
}

@Composable
private fun RateCell(label: String, base: RateBase?, modifier: Modifier = Modifier) {
    Column(modifier) {
        Text(label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline)
        val has = base != null && base.games > 0
        Text(
            if (has) base!!.rateText else "—",
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.Bold,
            color = if (has) rateColor(base!!.rate) else MaterialTheme.colorScheme.outline,
        )
        Text(
            if (!has) "无样本" else buildString {
                append("${base!!.wins}胜 ${base.losses}负")
                if (base.draws > 0) append(" ${base.draws}平")
            },
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
        )
    }
}

@Composable
private fun MatchupLine(row: MatchupRow) {
    val color = rateColor(row.rate)
    Column(
        Modifier
            .fillMaxWidth()
            .padding(vertical = 3.dp)
            .alpha(if (row.lowSample) 0.5f else 1f),
    ) {
        Row(verticalAlignment = Alignment.Top) {
            Text(
                row.deck,
                style = MaterialTheme.typography.bodySmall,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            Text(
                row.rateText,
                style = MaterialTheme.typography.bodySmall,
                fontWeight = FontWeight.Bold,
                color = color,
                textAlign = TextAlign.End,
                modifier = Modifier.width(56.dp),
            )
            Text(
                "${row.games} 局",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
                textAlign = TextAlign.End,
                modifier = Modifier.width(44.dp),
            )
        }
        Spacer(Modifier.height(2.dp))
        RateBar(((row.rate ?: 0.0) / 100.0).toFloat(), Modifier.fillMaxWidth(), color)
    }
}

// ---------- 单卡流向 ----------

@Composable
private fun FlowBlock(state: Load<CardFlow>, retry: () -> Unit) {
    SectionCard("单卡流向") {
        SectionBody(state, retry) { f ->
            val decks = carryingDecks(f.items)
            if (decks.isEmpty()) {
                Muted("前百里没有卡组带这张卡。")
                return@SectionBody
            }
            Text(
                "${f.totalUsed} 人带，分布在 ${decks.size} 个卡组（主卡 ${f.mainUsed} 人 · Side ${f.sideUsed} 人）",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
            Spacer(Modifier.height(4.dp))
            decks.take(12).forEach { FlowLine(it) }
            if (decks.size > 12) Muted("…另有 ${decks.size - 12} 个卡组")
        }
    }
}

/** 一个卡组一行：「盈彩月夜 · 2/3 人带（67%）」+ 细条 + 主卡 / Side 人数。 */
@Composable
private fun FlowLine(d: FlowDeck) {
    val denom = if (d.total.known > 0) d.total.known else d.players
    Column(Modifier.fillMaxWidth().padding(vertical = 3.dp)) {
        Row(verticalAlignment = Alignment.Top, horizontalArrangement = Arrangement.SpaceBetween) {
            Text(
                d.deck,
                style = MaterialTheme.typography.bodySmall,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            Text(
                "${d.total.used}/$denom 人 · ${d.total.percentText}",
                style = MaterialTheme.typography.labelSmall,
                fontWeight = FontWeight.Bold,
            )
        }
        Spacer(Modifier.height(2.dp))
        RateBar((d.total.rate ?: 0.0).toFloat(), Modifier.fillMaxWidth())
        val parts = listOfNotNull(
            d.main.used.takeIf { it > 0 }?.let { "主卡 $it 人" },
            d.side.used.takeIf { it > 0 }?.let { "Side $it 人" },
        )
        if (parts.isNotEmpty()) {
            Text(
                parts.joinToString(" · "),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
    }
}
