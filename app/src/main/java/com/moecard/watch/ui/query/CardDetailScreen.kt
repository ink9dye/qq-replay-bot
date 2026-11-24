package com.moecard.watch.ui.query

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.moecard.watch.net.AuthException
import com.moecard.watch.query.model.CardDetail
import com.moecard.watch.query.net.CardTextApi
import com.moecard.watch.query.net.QuerySession
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 卡片详情：search?type=card 命中后进入。details 透传自查卡源（含卡名 / 类型 / 效果），
 * metrics 为前百构筑统计（携带率 / 胜率 / by_deck），统计不可用时为 null，整块不显示。
 *
 * 2.2.0：顶部加「关键数字」（携带率 / 胜率 / 先后攻），类型行缺失时用百鸽补上，
 * 长效果默认折叠，去掉了空的 metrics 提示和「条件筛选」占位。
 */
class CardDetailViewModel : ViewModel() {
    val state = MutableStateFlow<UiState>(UiState.Loading)

    sealed interface UiState {
        object Loading : UiState
        data class Ready(val card: CardDetail) : UiState
        data class Failed(val message: String) : UiState
    }

    private var loadedKey: Long = 0

    fun load(code: Long) {
        if (loadedKey == code) return
        loadedKey = code
        state.value = UiState.Loading
        viewModelScope.launch {
            try {
                val card = withContext(Dispatchers.IO) { withYgocdbText(QuerySession.api().card(code)) }
                state.value = UiState.Ready(card)
            } catch (e: AuthException) {
                QuerySession.expire()
            } catch (e: Exception) {
                state.value = UiState.Failed(e.message ?: "加载失败")
            }
        }
    }

    /** 自建接口不返回类型（有时也缺效果），用百鸽补齐；百鸽不可用时原样返回。 */
    private fun withYgocdbText(card: CardDetail): CardDetail {
        if (card.types != null && card.desc != null) return card
        val extra = try {
            CardTextApi.cached(card.code) ?: CardTextApi.fetch(card.code)
        } catch (_: Exception) {
            null
        } ?: return card
        return card.copy(
            name = card.name ?: extra.name,
            types = card.types ?: extra.types,
            desc = card.desc ?: extra.desc,
            pdesc = card.pdesc ?: extra.pdesc,
        )
    }
}

@Composable
fun CardDetailScreen(code: Long, presetName: String?) {
    val vm: CardDetailViewModel = viewModel(key = "card-$code")
    LaunchedEffect(code) { vm.load(code) }
    val state by vm.state.collectAsStateWithLifecycle()
    val scroll = rememberScrollState()
    // 三个统计接口与卡片详情排在同一条查询队列里，顶部关键数字和下方明细共用这份状态
    val stats = rememberCardStats(code)

    when (val s = state) {
        CardDetailViewModel.UiState.Loading -> LoadingView("正在查询卡片…")
        is CardDetailViewModel.UiState.Failed -> ErrorView(s.message) { vm.load(code) }
        is CardDetailViewModel.UiState.Ready -> {
            val c = s.card
            Column(Modifier.fillMaxSize().verticalScroll(scroll).padding(16.dp)) {
                Row {
                    CardImage(
                        code = code,
                        contentScale = ContentScale.Fit,
                        modifier = Modifier
                            .height(150.dp)
                            .width(103.dp)
                            .clip(RoundedCornerShape(8.dp)),
                    )
                    Spacer(Modifier.width(12.dp))
                    Column {
                        Text(
                            c.name ?: presetName ?: "未知卡片",
                            style = MaterialTheme.typography.titleLarge,
                            fontWeight = FontWeight.Bold,
                        )
                        if (c.types != null) {
                            Spacer(Modifier.height(2.dp))
                            Text(
                                c.types.replace("\n", "  "),
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                        Spacer(Modifier.height(2.dp))
                        Text(
                            "卡号 $code",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.outline,
                        )
                    }
                }

                CardKeyNumbers(stats)

                // 效果文本（长文默认折叠）
                if (c.pdesc != null && c.pdesc.isNotBlank()) {
                    EffectCard("灵摆效果", c.pdesc)
                }
                if (c.desc != null && c.desc.isNotBlank()) {
                    EffectCard(
                        if (c.pdesc != null && c.pdesc.isNotBlank()) "怪兽效果" else "卡片效果",
                        c.desc,
                    )
                }

                // 旧的 metrics 统计：服务端给了才显示，空的时候整块不出现
                if (c.metrics != null) {
                    Spacer(Modifier.height(8.dp))
                    Text("前百构筑统计", style = MaterialTheme.typography.titleMedium)
                    Spacer(Modifier.height(4.dp))
                    RateBlock("主卡组", metricsOf(c, "main"))
                    RateBlock("副卡组", metricsOf(c, "side"))
                    RateBlock("额外卡组", metricsOf(c, "extra"))
                    ByDeckBlock(c)
                }

                CardStatsSection(stats)
                Spacer(Modifier.height(24.dp))
            }
        }
    }
}

private fun metricsOf(c: CardDetail, partition: String): org.json.JSONObject? =
    c.metrics?.optJSONObject(partition)

private const val EFFECT_FOLD_CHARS = 140

@Composable
private fun EffectCard(title: String, text: String) {
    var expanded by remember(text) { mutableStateOf(false) }
    val long = text.length > EFFECT_FOLD_CHARS
    Card(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Text(title, style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.height(4.dp))
            Text(
                text,
                style = MaterialTheme.typography.bodySmall,
                maxLines = if (long && !expanded) 5 else Int.MAX_VALUE,
                overflow = TextOverflow.Ellipsis,
            )
            if (long) {
                TextButton(onClick = { expanded = !expanded }, contentPadding = PaddingValues(0.dp)) {
                    Text(if (expanded) "收起" else "展开全文")
                }
            }
        }
    }
}

/** by_deck：携带分布（每项 {deck_type, metrics}）。 */
@Composable
private fun ByDeckBlock(c: CardDetail) {
    val byDeck = c.metrics?.optJSONArray("by_deck") ?: return
    if (byDeck.length() == 0) return
    Card(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Text("按卡组分布", style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.height(4.dp))
            if (byDeck.length() > 12) {
                Text(
                    "共 ${byDeck.length()} 个卡组，这里只列前 12 个",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                )
            }
            for (i in 0 until minOf(byDeck.length(), 12)) {
                val item = byDeck.optJSONObject(i) ?: continue
                val deck = item.optString("deck_type", "?")
                val m = item.optJSONObject("metrics") ?: continue
                val carry = m.optJSONObject("carrying")
                val pct = carry?.optDouble("percentage", Double.NaN)
                val label = if (pct == null || pct.isNaN()) "" else String.format("%.1f%%", pct)
                Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
                    Text(deck, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
                    Text(
                        label,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.primary,
                    )
                }
            }
        }
    }
}
