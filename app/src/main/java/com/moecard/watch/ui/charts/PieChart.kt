package com.moecard.watch.ui.charts

import android.graphics.drawable.BitmapDrawable
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.FilterQuality
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Constraints
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.graphics.drawable.toBitmap
import coil.imageLoader
import coil.request.ImageRequest
import com.moecard.watch.query.model.DeckThumbs
import com.moecard.watch.query.model.PieCategory
import com.moecard.watch.query.model.PieDeck
import com.moecard.watch.query.model.PieLayers
import com.moecard.watch.query.model.PieMiddle
import com.moecard.watch.query.model.PieSlice
import com.moecard.watch.ui.query.ArtworkImage
import com.moecard.watch.ui.query.artworkCrop
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.roundToInt
import kotlin.math.sin

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

// ---------- 前百三层饼图（口径与服务端 bot/deck_pie.py / 统计网页一致） ----------

private fun hexColor(hex: String, fallback: Color): Color = try {
    Color(android.graphics.Color.parseColor(hex))
} catch (_: Exception) {
    fallback
}

private fun pct(v: Double): String = String.format(java.util.Locale.US, "%.1f%%", v)

private fun categoryRank(name: String): Int = when {
    name.contains("未识别") -> 2
    name.contains("其他") -> 1
    else -> 0
}

/** 大类排序：按人数降序，但「其他」放倒数第二、「未识别」放最后（与服务端展示口径一致）。 */
internal fun pieCategoriesOrdered(list: List<PieCategory>): List<PieCategory> =
    list.filter { it.players > 0 }
        .sortedWith(compareBy({ categoryRank(it.name) }, { -it.players }))

private data class CatArc(val cat: PieCategory, val start: Float, val sweep: Float)

private fun catArcs(cats: List<PieCategory>, total: Int): List<CatArc> {
    if (total <= 0) return emptyList()
    var angle = -90f
    return cats.map { cat ->
        val sweep = 360f * cat.players / total
        CatArc(cat, angle, sweep).also { angle += sweep }
    }
}

/** 服务端“其他”大类不带 image 时，沿用统计网页的兜底代表卡（莫忘天魔女）。 */
private const val OTHER_FALLBACK_IMAGE = 54550967L

private fun PieCategory.repImage(): Long? =
    image ?: bandImage ?: if (name.contains("其他")) OTHER_FALLBACK_IMAGE else null

/** 预加载每个大类的代表卡整图，供扇区贴片（Coil 下载后转 ImageBitmap）。 */
@Composable
private fun rememberCategoryBitmaps(cats: List<PieCategory>): Map<Long, ImageBitmap> {
    val ctx = LocalContext.current
    val map = remember { mutableStateMapOf<Long, ImageBitmap>() }
    val codes = remember(cats) { cats.mapNotNull { it.repImage() }.distinct() }
    LaunchedEffect(codes) {
        codes.forEach { code ->
            if (map[code] == null) {
                runCatching {
                    val req = ImageRequest.Builder(ctx)
                        .data(DeckThumbs.cardImageUrl(code))
                        .allowHardware(false)
                        .build()
                    val drawable = ctx.imageLoader.execute(req).drawable
                    val bmp = (drawable as? BitmapDrawable)?.bitmap ?: drawable?.toBitmap()
                    if (bmp != null) map[code] = bmp.asImageBitmap()
                }
            }
        }
    }
    return map
}

/**
 * 单环主大类饼：扇区整块贴代表卡卡图（参考统计网页渲染），外圈引线标签，中心显示总人数。
 * 点扇区回调该大类下人数最多的卡组。
 */
@Composable
fun DeckPieChart(
    layers: PieLayers,
    modifier: Modifier = Modifier,
    onDeckClick: ((PieDeck) -> Unit)? = null,
) {
    if (layers.total <= 0 || layers.inner.isEmpty()) {
        Text("暂无统计数据", style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.outline, modifier = Modifier.padding(vertical = 24.dp))
        return
    }
    val cats = pieCategoriesOrdered(layers.inner)
    val arcs = catArcs(cats, layers.total)
    val bitmaps = rememberCategoryBitmaps(cats)
    val fallbackInner = MaterialTheme.colorScheme.surfaceVariant
    val onSurface = MaterialTheme.colorScheme.onSurface
    val measurer = rememberTextMeasurer()
    Box(
        modifier = modifier.fillMaxWidth().height(300.dp),
        contentAlignment = Alignment.Center,
    ) {
        Canvas(
            modifier = Modifier
                .fillMaxSize()
                .pointerInput(arcs) {
                    detectTapGestures { pos ->
                        if (onDeckClick == null || arcs.isEmpty()) return@detectTapGestures
                        val cx = size.width / 2f
                        val cy = size.height / 2f
                        val dx = pos.x - cx
                        val dy = pos.y - cy
                        val r = hypot(dx.toDouble(), dy.toDouble()).toFloat()
                        val radius = minOf(size.width / 2f - 76.dp.toPx(), size.height / 2f - 10.dp.toPx())
                        if (r < radius * 0.38f || r > radius) return@detectTapGestures
                        var deg = Math.toDegrees(atan2(dx.toDouble(), -dy.toDouble())).toFloat()
                        if (deg < 0f) deg += 360f
                        val arc = arcs.firstOrNull { deg >= it.start + 90f && deg < it.start + it.sweep + 90f }
                            ?: return@detectTapGestures
                        layers.outer
                            .filter { it.primary == arc.cat.name && it.players > 0 }
                            .maxByOrNull { it.players }
                            ?.let { onDeckClick(it) }
                    }
                }
        ) {
            val cx = center.x
            val cy = center.y
            val R = minOf(size.width / 2f - 76.dp.toPx(), size.height / 2f - 10.dp.toPx())
            val innerR = R * 0.38f
            val outerRect = Rect(cx - R, cy - R, cx + R, cy + R)
            val innerRect = Rect(cx - innerR, cy - innerR, cx + innerR, cy + innerR)
            arcs.forEach { a ->
                val path = Path().apply {
                    arcTo(outerRect, a.start, a.sweep, true)
                    arcTo(innerRect, a.start + a.sweep, -a.sweep, false)
                    close()
                }
                drawPath(path, hexColor(a.cat.color, fallbackInner))
                val bmp = a.cat.repImage()?.let { bitmaps[it] }
                if (bmp != null) {
                    // 只取插画区域（去掉卡框和效果框），中心裁成正方形铺满扇区
                    val crop = artworkCrop(bmp.width, bmp.height)
                    val side = minOf(crop.width, crop.height)
                    val sx = crop.left + (crop.width - side) / 2
                    val sy = crop.top + (crop.height - side) / 2
                    drawContext.canvas.save()
                    drawContext.canvas.clipPath(path)
                    drawImage(
                        image = bmp,
                        srcOffset = IntOffset(sx.roundToInt(), sy.roundToInt()),
                        srcSize = IntSize(side.roundToInt(), side.roundToInt()),
                        dstOffset = IntOffset((cx - R).roundToInt(), (cy - R).roundToInt()),
                        dstSize = IntSize((2 * R).roundToInt(), (2 * R).roundToInt()),
                        filterQuality = FilterQuality.Medium,
                    )
                    drawContext.canvas.restore()
                }
                drawPath(path, Color.White, style = Stroke(width = 1.2.dp.toPx()))
            }
            // 中心白底
            drawCircle(Color.White, innerR, Offset(cx, cy))
            drawCircle(Color(0xFFE0E4E8), innerR, Offset(cx, cy), style = Stroke(1.dp.toPx()))
            // 外圈标签（<5% 的小片不标；接近上下方向时改放正上/正下，避免文字压到扇区）
            arcs.filter { it.cat.percent >= 5.0 }.forEach { a ->
                val mid = Math.toRadians((a.start + a.sweep / 2).toDouble())
                val cosv = cos(mid).toFloat()
                val sinv = sin(mid).toFloat()
                val ax = cx + cosv * (R + 12.dp.toPx())
                val ay = cy + sinv * (R + 12.dp.toPx())
                drawCircle(hexColor(a.cat.color, Color.Gray), 3.dp.toPx(), Offset(ax, ay))
                val layout = measurer.measure(
                    AnnotatedString("${a.cat.name} ${pct(a.cat.percent)}"),
                    style = TextStyle(fontSize = 10.sp, color = onSurface),
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    constraints = Constraints(maxWidth = (size.width / 2f - 16.dp.toPx()).toInt()),
                )
                val w = layout.size.width
                val h = layout.size.height
                val tx = when {
                    cosv >= 0.4f -> ax + 8.dp.toPx()
                    cosv <= -0.4f -> ax - 8.dp.toPx() - w
                    else -> ax - w / 2f
                }
                val ty = when {
                    sinv <= -0.4f -> ay - 10.dp.toPx() - h
                    sinv >= 0.4f -> ay + 10.dp.toPx()
                    else -> ay - h / 2f
                }
                drawText(layout, topLeft = Offset(tx, ty))
            }
        }
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text("${layers.total}", style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.SemiBold)
            Text("总人数 单环 100%", style = MaterialTheme.typography.labelSmall,
                fontSize = 9.sp,
                color = MaterialTheme.colorScheme.outline)
        }
    }
}

/**
 * 三层图例：大类（代表卡图）→ 交集细分（中环数据）→ 具体卡组（代表卡图 + 可点击）。
 * 代表卡全部用服务端 `image`（组内代表卡 / 精确卡组投入最多的主卡），不再走本地静态映射。
 */
@Composable
fun PieLayersLegend(
    layers: PieLayers,
    modifier: Modifier = Modifier,
    onDeckClick: ((PieDeck) -> Unit)? = null,
) {
    Column(modifier = modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(2.dp)) {
        pieCategoriesOrdered(layers.inner).forEach { cat ->
            CategoryRow(cat)
            layers.middle.filter { it.primary == cat.name }.forEach { MiddleRow(it) }
            layers.outer.filter { it.primary == cat.name }.forEachIndexed { index, deck ->
                DeckRow(index + 1, deck, onDeckClick)
            }
        }
        if (layers.note.isNotBlank()) {
            Text(layers.note, style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline, modifier = Modifier.padding(top = 8.dp))
        }
    }
}

@Composable
private fun CategoryRow(cat: PieCategory) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(top = 10.dp, bottom = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier
                .size(12.dp)
                .clip(CircleShape)
                .background(hexColor(cat.color, Color.Gray))
        )
        Spacer(Modifier.width(8.dp))
        Text(
            "${cat.name} · ${cat.players}人 · ${pct(cat.percent)}",
            style = MaterialTheme.typography.titleSmall,
            fontWeight = FontWeight.SemiBold,
        )
        Spacer(Modifier.weight(1f))
        val img = cat.repImage()
        if (img != null) {
            ArtworkImage(img, Modifier.size(48.dp, 32.dp).clip(RoundedCornerShape(4.dp)))
        }
    }
}

@Composable
private fun MiddleRow(m: PieMiddle) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(start = 20.dp, top = 1.dp, bottom = 1.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier
                .size(8.dp)
                .clip(CircleShape)
                .background(hexColor(m.mixed ?: m.accent ?: m.color, Color.LightGray))
        )
        Spacer(Modifier.width(6.dp))
        Text(
            "${m.label} · ${m.players}人 · ${pct(m.percent)}",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        if (m.image != null) {
            Spacer(Modifier.width(8.dp))
            ArtworkImage(m.image, Modifier.size(32.dp, 22.dp).clip(RoundedCornerShape(3.dp)))
        }
    }
}

@Composable
private fun DeckRow(index: Int, deck: PieDeck, onDeckClick: ((PieDeck) -> Unit)?) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .then(if (onDeckClick != null) Modifier.clickable { onDeckClick(deck) } else Modifier)
            .padding(start = 20.dp, top = 3.dp, bottom = 3.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            "$index",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
            modifier = Modifier.width(16.dp),
        )
        if (deck.image != null) {
            ArtworkImage(deck.image, Modifier.size(36.dp, 24.dp).clip(RoundedCornerShape(3.dp)))
            Spacer(Modifier.width(8.dp))
        }
        Column {
            Text(
                deck.name,
                style = MaterialTheme.typography.bodySmall,
                fontWeight = FontWeight.Medium,
            )
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    "${deck.players}人 · ${pct(deck.percent)}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
                if (deck.extras.isNotEmpty()) {
                    Spacer(Modifier.width(6.dp))
                    Box(
                        Modifier
                            .size(6.dp)
                            .clip(CircleShape)
                            .background(hexColor(deck.accent ?: deck.color, Color.LightGray))
                    )
                    Spacer(Modifier.width(3.dp))
                    Text("∩${deck.extras.joinToString("、")}",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline)
                }
            }
        }
        if (onDeckClick != null) {
            Spacer(Modifier.weight(1f))
            Text("›", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.outline)
        }
    }
}
