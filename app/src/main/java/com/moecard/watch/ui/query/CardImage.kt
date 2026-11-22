package com.moecard.watch.ui.query

import android.graphics.drawable.BitmapDrawable
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.FilterQuality
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.core.graphics.drawable.toBitmap
import coil.compose.AsyncImage
import coil.imageLoader
import coil.request.ImageRequest
import com.moecard.watch.query.model.CardDetail
import com.moecard.watch.query.model.DeckThumbs
import java.util.concurrent.ConcurrentHashMap
import kotlin.math.roundToInt

/**
 * 统一卡图组件：momobako 主源，404 时自动切 cdntx super-pre。
 *
 * 新卡（例如 101307067）在 momobako 上是 404、super-pre 上才有；
 * 两个源都没有时保留灰底 + 卡号，不再出现纯空白。
 */
@Composable
fun CardImage(
    code: Long,
    modifier: Modifier = Modifier,
    contentScale: ContentScale = ContentScale.Fit,
) {
    var tryFallback by remember(code) { mutableStateOf(false) }
    val url = remember(code, tryFallback) {
        if (tryFallback) DeckThumbs.fallbackCardImageUrl(code) else DeckThumbs.cardImageUrl(code)
    }
    Box(
        modifier = modifier.background(MaterialTheme.colorScheme.surfaceVariant),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            code.toString(),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.outline,
        )
        AsyncImage(
            model = url,
            contentDescription = "卡图 $code",
            contentScale = contentScale,
            modifier = Modifier.fillMaxSize(),
            onError = { if (!tryFallback) tryFallback = true },
        )
    }
}

/** 整张卡图里插画窗口的相对区域：裁掉卡框、卡名和效果文本框，只留插画。 */
internal data class ArtworkCrop(val left: Float, val top: Float, val width: Float, val height: Float)

internal fun artworkCrop(w: Int, h: Int) = ArtworkCrop(
    left = w * 0.115f,
    top = h * 0.185f,
    width = w * 0.77f,
    height = h * 0.48f,
)

/**
 * 只显示卡图（插画区域）的组件：加载整张卡后按插画窗口中心裁切填满，
 * 不出现卡框、效果框与卡名文本，视觉与统计网页饼图贴片一致。
 */
@Composable
fun ArtworkImage(
    code: Long,
    modifier: Modifier = Modifier,
) {
    val ctx = LocalContext.current
    var tryFallback by remember(code) { mutableStateOf(false) }
    var bitmap by remember(code) { mutableStateOf<ImageBitmap?>(null) }
    val url = remember(code, tryFallback) {
        if (tryFallback) DeckThumbs.fallbackCardImageUrl(code) else DeckThumbs.cardImageUrl(code)
    }
    LaunchedEffect(url) {
        bitmap = null
        val drawable = runCatching {
            ctx.imageLoader.execute(
                ImageRequest.Builder(ctx).data(url).allowHardware(false).build()
            ).drawable
        }.getOrNull()
        val bmp = (drawable as? BitmapDrawable)?.bitmap ?: drawable?.toBitmap()
        if (bmp != null) {
            bitmap = bmp.asImageBitmap()
        } else if (!tryFallback) {
            tryFallback = true
        }
    }
    Box(
        modifier = modifier.background(MaterialTheme.colorScheme.surfaceVariant),
        contentAlignment = Alignment.Center,
    ) {
        val bmp = bitmap
        if (bmp != null) {
            Canvas(Modifier.fillMaxSize()) {
                if (size.width <= 0f || size.height <= 0f) return@Canvas
                val crop = artworkCrop(bmp.width, bmp.height)
                val srcAspect = crop.width / crop.height
                val dstAspect = size.width / size.height
                var sx = crop.left
                var sy = crop.top
                var sw = crop.width
                var sh = crop.height
                if (srcAspect > dstAspect) {
                    sw = crop.height * dstAspect
                    sx = crop.left + (crop.width - sw) / 2
                } else {
                    sh = crop.width / dstAspect
                    sy = crop.top + (crop.height - sh) / 2
                }
                drawImage(
                    image = bmp,
                    srcOffset = IntOffset(sx.roundToInt(), sy.roundToInt()),
                    srcSize = IntSize(sw.roundToInt(), sh.roundToInt()),
                    dstOffset = IntOffset.Zero,
                    dstSize = IntSize(size.width.roundToInt(), size.height.roundToInt()),
                    filterQuality = FilterQuality.Medium,
                )
            }
        } else {
            Text(
                code.toString(),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.outline,
            )
        }
    }
}

/** 卡片详情缓存：构筑页点开同一张卡不重复打查询接口。 */
object CardDetailCache {
    private val map = ConcurrentHashMap<Long, CardDetail>()

    fun get(code: Long): CardDetail? = map[code]

    fun put(code: Long, detail: CardDetail) {
        map[code] = detail
    }
}
