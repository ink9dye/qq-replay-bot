package com.moecard.watch.ui.charts

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.background
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.moecard.watch.query.model.PieSlice

/** 饼图调色板：与 Material 主色系搭配的一组固定色。 */
val PiePalette = listOf(
    Color(0xFF1E88E5), Color(0xFFE53935), Color(0xFF43A047), Color(0xFFFB8C00),
    Color(0xFF8E24AA), Color(0xFF00ACC1), Color(0xFF6D4C41), Color(0xFFC0CA33),
    Color(0xFFD81B60), Color(0xFF5E35B1), Color(0xFF00897B), Color(0xFFF4511E),
)

/**
 * 前百构筑分布饼图（Canvas 手绘）。图例可点击（onSliceClick），
 * 供「点击查看更多 → 卡组详情」使用。
 */
@Composable
fun PieChart(
    slices: List<PieSlice>,
    modifier: Modifier = Modifier,
    onSliceClick: ((PieSlice) -> Unit)? = null,
) {
    val known = slices.filter { it.count > 0 }
    val total = known.sumOf { it.count }
    Column(modifier = modifier.fillMaxWidth()) {
        if (total <= 0) {
            Text("暂无统计数据", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline, modifier = Modifier.padding(vertical = 24.dp))
            return@Column
        }
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(modifier = Modifier.size(160.dp), contentAlignment = Alignment.Center) {
                Canvas(modifier = Modifier.size(160.dp)) {
                    val diameter = size.minDimension * 0.86f
                    val topLeft = Offset((size.width - diameter) / 2f, (size.height - diameter) / 2f)
                    val rectSize = Size(diameter, diameter)
                    var start = -90f // 从 12 点方向开始
                    known.forEachIndexed { i, s ->
                        val sweep = 360f * s.count / total
                        drawArc(
                            color = PiePalette[i % PiePalette.size],
                            startAngle = start,
                            sweepAngle = sweep,
                            useCenter = true,
                            topLeft = topLeft,
                            size = rectSize,
                        )
                        start += sweep
                    }
                }
                Text(
                    "${total}人",
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onSurface,
                )
            }
            Spacer(Modifier.size(12.dp))
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                known.take(12).forEachIndexed { i, s ->
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = if (onSliceClick != null) {
                            Modifier
                                .clickable { onSliceClick(s) }
                                .padding(vertical = 2.dp)
                        } else {
                            Modifier.padding(vertical = 2.dp)
                        },
                    ) {
                        Box(
                            modifier = Modifier
                                .size(10.dp)
                                .clip(CircleShape)
                                .background(PiePalette[i % PiePalette.size])
                        )
                        Spacer(Modifier.size(6.dp))
                        Text(
                            if (onSliceClick != null) "${s.name} · ${s.count} ›"
                            else "${s.name} · ${s.count}",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurface,
                        )
                    }
                }
                if (known.size > 12) {
                    Text("…共${known.size}类", style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline)
                }
            }
        }
    }
}
