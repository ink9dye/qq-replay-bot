package com.moecard.watch.ui.charts

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.unit.dp

/**
 * DP 变化折线图（Compose Canvas 手绘，不引第三方图表库）。
 * 数据 = 首局赛前分 + 每局赛后分，与服务端 player_history.py 的积分走势同口径。
 */
@Composable
fun LineChart(
    points: List<Double>,
    modifier: Modifier = Modifier,
    lineColor: Color = MaterialTheme.colorScheme.primary,
    gridColor: Color = MaterialTheme.colorScheme.outlineVariant,
) {
    val labelColor = MaterialTheme.colorScheme.outline
    Column(modifier = modifier.fillMaxWidth()) {
        if (points.size < 2) {
            Text(
                if (points.isEmpty()) "暂无对局数据" else "对局不足，无法绘制曲线",
                style = MaterialTheme.typography.bodySmall,
                color = labelColor,
                modifier = Modifier.padding(vertical = 24.dp),
            )
            return@Column
        }
        Canvas(modifier = Modifier.fillMaxWidth().height(180.dp).padding(horizontal = 4.dp)) {
            val minV = points.min()
            val maxV = points.max()
            val span = (maxV - minV).takeIf { it > 1e-9 } ?: 1.0
            val w = size.width
            val h = size.height
            val padTop = 8f
            val padBottom = 8f

            fun x(i: Int): Float = if (points.size == 1) w / 2 else w * i / (points.size - 1).toFloat()
            fun y(v: Double): Float =
                (padTop + (h - padTop - padBottom) * (1f - ((v - minV) / span).toFloat()))

            // 横向网格：最大 / 最小两条参考线
            listOf(maxV, (maxV + minV) / 2, minV).forEach { v ->
                drawLine(
                    color = gridColor,
                    start = Offset(0f, y(v)),
                    end = Offset(w, y(v)),
                    strokeWidth = 1f,
                    pathEffect = PathEffect.dashPathEffect(floatArrayOf(6f, 8f)),
                )
            }
            // 折线
            val path = Path()
            points.forEachIndexed { i, v ->
                val px = x(i)
                val py = y(v)
                if (i == 0) path.moveTo(px, py) else path.lineTo(px, py)
            }
            drawPath(path, color = lineColor, style = Stroke(width = 4f))
            // 首尾点标记
            drawCircle(color = lineColor, radius = 7f, center = Offset(x(0), y(points.first())))
            drawCircle(color = lineColor, radius = 7f, center = Offset(x(points.lastIndex), y(points.last())))
        }
        Row(modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp)) {
            Text("第1局 · ${fmt(points.first())}", style = MaterialTheme.typography.labelSmall, color = labelColor)
            Spacer(Modifier.weight(1f))
            Text(
                "最近${points.size - 1}局 · ${fmt(points.last())}",
                style = MaterialTheme.typography.labelSmall,
                color = labelColor,
            )
        }
    }
}

private fun fmt(v: Double): String =
    if (v == v.toLong().toDouble()) v.toLong().toString() else String.format("%.1f", v)
