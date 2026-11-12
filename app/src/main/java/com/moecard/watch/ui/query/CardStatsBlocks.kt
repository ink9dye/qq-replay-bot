package com.moecard.watch.ui.query

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.moecard.watch.net.AuthException
import com.moecard.watch.query.model.CardFlow
import com.moecard.watch.query.model.CardUsage
import com.moecard.watch.query.model.CardWinrate
import com.moecard.watch.query.model.CountDist
import com.moecard.watch.query.model.FlowBucket
import com.moecard.watch.query.model.RateBase
import com.moecard.watch.query.net.QuerySession
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * 单卡统计三块：使用量 / 单卡胜率 / 单卡流向（数据来自
 * /query-api/card/{code}/(usage|flow|winrate)，口径为当前前百构筑）。
 *
 * 三个接口串行请求：服务端 heavy 闸门只有 2 路，客户端并发只会自己把自己挤成 429。
 * 每块独立成败，任何一块失败都不影响其他两块和卡片基础信息。
 */
@Composable
fun CardStatsSection(code: Long) {
    var usage by remember(code) { mutableStateOf<Load<CardUsage>>(Load.Loading) }
    var winrate by remember(code) { mutableStateOf<Load<CardWinrate>>(Load.Loading) }
    var flow by remember(code) { mutableStateOf<Load<CardFlow>>(Load.Loading) }
    var attempt by remember(code) { mutableStateOf(0) }

    LaunchedEffect(code, attempt) {
        usage = Load.Loading
        winrate = Load.Loading
        flow = Load.Loading
        usage = loadIo { QuerySession.api().cardUsage(code) }
        winrate = loadIo { QuerySession.api().cardWinrate(code) }
        flow = loadIo { QuerySession.api().cardFlow(code) }
    }

    Spacer(Modifier.height(8.dp))
    Text("单卡统计（当前前百）", style = MaterialTheme.typography.titleMedium)
    Spacer(Modifier.height(4.dp))

    UsageBlock(usage) { attempt++ }
    WinrateBlock(winrate) { attempt++ }
    FlowBlock(flow) { attempt++ }
}

// ---------- 每块的容器 ----------

private sealed interface Load<out T> {
    object Loading : Load<Nothing>
    data class Ok<T>(val value: T) : Load<T>
    data class Err(val message: String) : Load<Nothing>
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

@Composable
private fun SectionCard(title: String, content: @Composable () -> Unit) {
    Card(Modifier.fillMaxWidth().padding(vertical = 4.dp), colors = CardDefaults.outlinedCardColors()) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Text(title, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(6.dp))
            content()
        }
    }
}

@Composable
private fun <T> SectionBody(state: Load<T>, retry: () -> Unit, content: @Composable (T) -> Unit) {
    when (state) {
        Load.Loading -> Text(
            "统计中…",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.outline,
        )
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

// ---------- 使用量 ----------

@Composable
private fun UsageBlock(state: Load<CardUsage>, retry: () -> Unit) {
    SectionCard("使用量") {
        SectionBody(state, retry) { u ->
            if (u.current <= 0) {
                Text(
                    "前百构筑里没有携带这张卡的记录。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                return@SectionBody
            }
            Text(
                "覆盖 ${u.covered} / ${u.current} 人" +
                    (if (u.excluded > 0) "（另 ${u.excluded} 人无确认首局，未计入）" else ""),
                style = MaterialTheme.typography.bodySmall,
            )
            Text(
                "主卡合计 ${u.mainTotal} 张 · Side 换入 ${u.sideTotal} 张 · 总投入 ${u.totalTotal} 张（${u.sideUsed} 人换入过）",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
            Spacer(Modifier.height(6.dp))
            DistBlock("主卡组", u.mainDist)
            Spacer(Modifier.height(4.dp))
            DistBlock("Side（换入）", u.sideDist)
            Spacer(Modifier.height(4.dp))
            DistBlock("合计", u.totalDist)
            if (u.records.isNotEmpty()) {
                Spacer(Modifier.height(6.dp))
                Text(
                    "名单前 ${u.records.size} 人" + (if (u.truncated) "（共 ${u.recordsTotal} 人）" else ""),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                u.records.take(6).forEach { r ->
                    Row(Modifier.fillMaxWidth().padding(vertical = 1.dp)) {
                        Text(
                            "${if (r.rank > 0) "#${r.rank} " else ""}${r.player}",
                            style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.weight(1f),
                        )
                        Text(
                            "${r.main}+${r.side}=${r.total}",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.outline,
                        )
                    }
                }
            }
        }
    }
}

/** 0/1/2/3+ 张的人数分布；3 表示 3 张及以上。 */
@Composable
private fun DistBlock(label: String, dist: CountDist) {
    val total = dist.total
    Column(Modifier.fillMaxWidth()) {
        Text(label, style = MaterialTheme.typography.labelMedium)
        dist.buckets.forEach { (copies, count) ->
            val frac = if (total > 0) count.toFloat() / total else 0f
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    if (copies >= 3) "3+ 张" else "$copies 张",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                    modifier = Modifier.padding(end = 6.dp),
                )
                RateBar(frac, Modifier.weight(1f))
                Text(
                    " $count",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
        }
    }
}

@Composable
private fun RateBar(fraction: Float, modifier: Modifier = Modifier) {
    Box(
        modifier
            .height(6.dp)
            .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(3.dp)),
    ) {
        Box(
            Modifier
                .fillMaxWidth(fraction.coerceIn(0f, 1f))
                .height(6.dp)
                .background(MaterialTheme.colorScheme.primary, RoundedCornerShape(3.dp)),
        )
    }
}

// ---------- 单卡胜率 ----------

@Composable
private fun WinrateBlock(state: Load<CardWinrate>, retry: () -> Unit) {
    SectionCard("单卡胜率") {
        SectionBody(state, retry) { w ->
            val base = w.base
            if (base == null || base.games <= 0) {
                Text(
                    "没有“使用该卡且胜负明确”的小局。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                return@SectionBody
            }
            Row(Modifier.fillMaxWidth()) {
                RateCell("总体", base, Modifier.weight(1f))
                RateCell("先攻", w.first, Modifier.weight(1f))
                RateCell("后攻", w.second, Modifier.weight(1f))
            }
            Spacer(Modifier.height(6.dp))
            if (w.rows.isNotEmpty()) {
                Text(
                    "对阵卡组（前 ${w.rows.size} 个，共 ${w.totalDecks} 个）",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                w.rows.take(8).forEach { row ->
                    Row(Modifier.fillMaxWidth().padding(vertical = 1.dp)) {
                        Text(
                            if (row.lowSample) "${row.deck}（样本少）" else row.deck,
                            style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.weight(1f),
                        )
                        Text(
                            "${row.rateText} · ${row.games} 局",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.outline,
                        )
                    }
                }
            }
            if (w.notes.isNotEmpty()) {
                Spacer(Modifier.height(4.dp))
                w.notes.forEach {
                    Text(
                        "· $it",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                    )
                }
            }
        }
    }
}

@Composable
private fun RateCell(label: String, base: RateBase?, modifier: Modifier = Modifier) {
    Column(modifier) {
        Text(label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.outline)
        Text(
            base?.rateText ?: "—",
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.Bold,
        )
        Text(
            if (base == null || base.games <= 0) "无样本" else "${base.games} 局",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
        )
    }
}

// ---------- 单卡流向 ----------

@Composable
private fun FlowBlock(state: Load<CardFlow>, retry: () -> Unit) {
    SectionCard("单卡流向") {
        SectionBody(state, retry) { f ->
            if (f.items.isEmpty()) {
                Text(
                    "没有可用的卡组维度记录。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                return@SectionBody
            }
            Text(
                "覆盖 ${f.covered} / ${f.current} 人 · 主卡 ${f.mainUsed} 人 · Side ${f.sideUsed} 人 · 合计 ${f.totalUsed} 人",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
            Spacer(Modifier.height(4.dp))
            // 通用卡可能有上百个卡组，按人数取前 12 个，避免页面被拉成一张长表
            val shown = f.items.sortedByDescending { it.players }.take(12)
            if (f.items.size > shown.size) {
                Text(
                    "共 ${f.items.size} 个卡组，按人数只列前 ${shown.size} 个",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
            shown.forEach { item ->
                Column(Modifier.fillMaxWidth().padding(vertical = 3.dp)) {
                    Row(Modifier.fillMaxWidth()) {
                        Text(
                            item.deck,
                            style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.weight(1f),
                        )
                        Text(
                            "${item.players} 人",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.outline,
                        )
                    }
                    FlowLine("主卡", item.main)
                    FlowLine("Side", item.side)
                    FlowLine("合计", item.total)
                }
            }
        }
    }
}

@Composable
private fun FlowLine(label: String, bucket: FlowBucket) {
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Text(
            label,
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
            modifier = Modifier.padding(end = 6.dp),
        )
        RateBar((bucket.rate ?: 0.0).toFloat(), Modifier.weight(1f))
        Text(
            " ${bucket.percentText}",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
        )
    }
}
