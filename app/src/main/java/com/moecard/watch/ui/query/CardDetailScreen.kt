package com.moecard.watch.ui.query

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import coil.compose.AsyncImage
import com.moecard.watch.net.AuthException
import com.moecard.watch.query.model.CardDetail
import com.moecard.watch.query.net.QuerySession
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 卡片详情：search?type=card 命中后进入。details 透传自查卡源（含卡名 / 类型 / 效果），
 * metrics 为前百构筑统计（携带率 / 胜率 / by_deck），统计不可用时为 null，卡片信息仍展示。
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
                val card = withContext(Dispatchers.IO) { QuerySession.api().card(code) }
                state.value = UiState.Ready(card)
            } catch (e: AuthException) {
                QuerySession.expire()
            } catch (e: Exception) {
                state.value = UiState.Failed(e.message ?: "加载失败")
            }
        }
    }
}

@Composable
fun CardDetailScreen(code: Long, presetName: String?) {
    val vm: CardDetailViewModel = viewModel(key = "card-$code")
    LaunchedEffect(code) { vm.load(code) }
    val state by vm.state.collectAsStateWithLifecycle()
    val scroll = rememberScrollState()

    when (val s = state) {
        CardDetailViewModel.UiState.Loading -> LoadingView("正在查询卡片…")
        is CardDetailViewModel.UiState.Failed -> ErrorView(s.message) { vm.load(code) }
        is CardDetailViewModel.UiState.Ready -> {
            val c = s.card
            Column(Modifier.fillMaxSize().verticalScroll(scroll).padding(16.dp)) {
                Row {
                    if (c.imageUrl != null) {
                        AsyncImage(
                            model = c.imageUrl,
                            contentDescription = c.name,
                            contentScale = ContentScale.Fit,
                            modifier = Modifier
                                .height(170.dp)
                                .clip(RoundedCornerShape(8.dp)),
                        )
                        Spacer(Modifier.padding(8.dp))
                    }
                    Column(Modifier.padding(start = if (c.imageUrl != null) 8.dp else 0.dp)) {
                        Text(
                            c.name ?: presetName ?: "未知卡片",
                            style = MaterialTheme.typography.titleLarge,
                            fontWeight = FontWeight.Bold,
                        )
                        Text(
                            "卡号 $code",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.outline,
                        )
                        if (c.types != null) {
                            Text(
                                c.types,
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.outline,
                            )
                        }
                    }
                }
                Spacer(Modifier.height(12.dp))

                // 效果文本
                if (c.pdesc != null && c.pdesc.isNotBlank()) {
                    EffectCard("灵摆效果", c.pdesc)
                }
                if (c.desc != null && c.desc.isNotBlank()) {
                    EffectCard(
                        if (c.pdesc != null && c.pdesc.isNotBlank()) "怪兽效果" else "卡片效果",
                        c.desc,
                    )
                }

                Spacer(Modifier.height(8.dp))
                Text("前百构筑统计", style = MaterialTheme.typography.titleMedium)
                Spacer(Modifier.height(4.dp))
                if (c.metrics == null) {
                    Text(
                        "统计暂不可用（metrics 为空），卡片信息不受影响。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.outline,
                    )
                } else {
                    RateBlock("主卡组", metricsOf(c, "main"))
                    RateBlock("副卡组", metricsOf(c, "side"))
                    RateBlock("额外卡组", metricsOf(c, "extra"))
                    ByDeckBlock(c)
                }

                Spacer(Modifier.height(8.dp))
                ReservedCard(
                    "卡组胜率统计",
                    "按卡组维度查看携带该卡的卡组胜率排名。服务端暂无对应端点，等后续候选提供。",
                )
                ReservedCard(
                    "条件筛选",
                    "按时间 / 段位等条件过滤统计。查询接口暂不支持筛选参数。",
                )
                Spacer(Modifier.height(24.dp))
            }
        }
    }
}

private fun metricsOf(c: CardDetail, partition: String): org.json.JSONObject? =
    c.metrics?.optJSONObject(partition)

@Composable
private fun EffectCard(title: String, text: String) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        colors = CardDefaults.outlinedCardColors(),
    ) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Text(title, style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.height(4.dp))
            Text(text, style = MaterialTheme.typography.bodySmall)
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
            for (i in 0 until byDeck.length()) {
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
