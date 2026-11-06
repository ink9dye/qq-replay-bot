package com.moecard.watch.ui.query

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import org.json.JSONArray
import org.json.JSONObject

/**
 * 兜底渲染与通用状态组件。
 *
 * 自建查询 API 有不少门户透传字段，形状只以服务端源码为准、未必与文档完全一致；
 * 这里提供：加载 / 错误 / 空态、通用 JSON 表格、metrics 的 fraction/rate 渲染，
 * 以及「暂未开放」的预留入口（卡组胜率、ROTK、条件筛选——服务端无对应端点）。
 */

@Composable
fun LoadingView(hint: String = "加载中…") {
    Column(
        modifier = Modifier.fillMaxWidth().padding(vertical = 40.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        CircularProgressIndicator()
        Spacer(Modifier.height(12.dp))
        Text(hint, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline)
    }
}

@Composable
fun ErrorView(message: String, retry: (() -> Unit)? = null) {
    Column(
        modifier = Modifier.fillMaxWidth().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("加载失败", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.error)
        Spacer(Modifier.height(6.dp))
        Text(message, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline)
        if (retry != null) {
            Spacer(Modifier.height(12.dp))
            OutlinedButton(onClick = retry) { Text("重试") }
        }
    }
}

@Composable
fun EmptyView(message: String) {
    Column(
        modifier = Modifier.fillMaxWidth().padding(vertical = 40.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(message, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline)
    }
}

/** 预留功能入口：服务端暂无端点的功能，界面占位并注明，后端就绪后补实现。 */
@Composable
fun ReservedCard(title: String, desc: String) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(title, style = MaterialTheme.typography.titleSmall)
                Spacer(Modifier.width(8.dp))
                ReservedChip()
            }
            Spacer(Modifier.height(4.dp))
            Text(desc, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline)
        }
    }
}

@Composable
fun ReservedChip() {
    Surface(
        color = MaterialTheme.colorScheme.surfaceVariant,
        shape = MaterialTheme.shapes.small,
    ) {
        Text(
            "暂未开放",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
        )
    }
}

/**
 * 通用 JSON 表格兜底：任何没被专用 UI 覆盖的 JSON（对象 / 数组）都能显示出来，
 * 保证「界面不崩、内容可见」，拿到真实响应后再补充专用渲染。
 */
@Composable
fun JsonTableView(value: Any?, level: Int = 0) {
    when (value) {
        null -> Text("（空）", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline)
        is JSONObject -> {
            if (value.length() == 0) {
                Text("（空对象）", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline)
                return
            }
            value.keys().forEach { k ->
                val v = value.opt(k)
                Row(modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
                    Text(
                        k,
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.width(120.dp),
                    )
                    if (v is JSONObject || v is JSONArray) {
                        Column(modifier = Modifier.padding(start = if (level == 0) 0.dp else 8.dp)) {
                            JsonTableView(v, level + 1)
                        }
                    } else {
                        Text(prettyScalar(v), style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        }
        is JSONArray -> {
            if (value.length() == 0) {
                Text("（空列表）", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline)
                return
            }
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                for (i in 0 until value.length()) {
                    Row(verticalAlignment = Alignment.Top) {
                        Text(
                            "${i + 1}.",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.outline,
                        )
                        Spacer(Modifier.width(6.dp))
                        JsonTableView(value.opt(i), level + 1)
                    }
                }
            }
        }
        else -> Text(prettyScalar(value), style = MaterialTheme.typography.bodySmall)
    }
}

private fun prettyScalar(v: Any?): String = when (v) {
    is String -> v
    is Boolean -> if (v) "是" else "否"
    is Double -> if (v == v.toLong().toDouble()) v.toLong().toString() else v.toString()
    else -> v?.toString() ?: "（空）"
}

/**
 * metrics 里的 fraction 结构：{numerator, denominator, percentage, ...}。
 * 统一渲染成「xx.x%（a/b）」。
 */
@Composable
fun FractionView(label: String, fraction: JSONObject?) {
    if (fraction == null) return
    val pct = fraction.optDouble("percentage", Double.NaN)
    val num = fraction.optLong("numerator", 0L)
    val den = fraction.optLong("denominator", 0L)
    val pctText = if (pct.isNaN()) "—" else if (pct == pct.toLong().toDouble()) {
        "${pct.toLong()}%"
    } else String.format("%.1f%%", pct)
    Row(modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
        Text(label, style = MaterialTheme.typography.labelMedium, modifier = Modifier.width(120.dp))
        Text("$pctText（$num/$den）", style = MaterialTheme.typography.bodySmall)
    }
}

/**
 * 卡片 metrics 的 rate 块（对照 bot/card_statistics.py 的 rates() 结构）：
 * 携带率 / 胜率 / 抽卡率 / 使用率 等；字段缺失自动跳过，不做任何假设。
 */
@Composable
fun RateBlock(title: String, metrics: JSONObject?) {
    if (metrics == null) return
    val known = listOf(
        "carrying" to "携带率",
        "scope_win_rate" to "对局胜率",
        "carried_win_rate" to "携带后胜率",
        "draw_rate" to "抽到率",
        "use_rate" to "使用率",
        "use_after_draw_rate" to "抽后使用率",
        "drawn_win_rate" to "抽到后胜率",
        "used_win_rate" to "使用后胜率",
        "appearance_rate" to "登场率（额外卡组）",
        "appeared_win_rate" to "登场后胜率",
    )
    val rows = known.mapNotNull { (key, label) ->
        metrics.optJSONObject(key)?.let { label to it }
    }
    if (rows.isEmpty()) return
    Card(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
            Text(title, style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.height(4.dp))
            HorizontalDivider()
            Spacer(Modifier.height(4.dp))
            rows.forEach { (label, fraction) -> FractionView(label, fraction) }
            val samples = metrics.optJSONObject("scope_win_rate")?.optLong("denominator")
                ?: metrics.optJSONObject("carrying")?.optLong("denominator")
            if (samples != null) {
                Text(
                    "样本数 $samples",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                    fontFamily = FontFamily.Monospace,
                )
            }
        }
    }
}
