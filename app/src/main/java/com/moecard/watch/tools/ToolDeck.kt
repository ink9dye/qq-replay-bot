package com.moecard.watch.tools

import com.moecard.watch.query.model.BuildData
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** 计算器使用的卡组：只有卡号（按张数重复），卡片属性由 [CardInfoRepo] 按需查询。 */
data class ToolDeck(
    val label: String,
    val main: List<Long>,
    val extra: List<Long>,
) {
    val isEmpty: Boolean get() = main.isEmpty() && extra.isEmpty()

    companion object {
        /** 从查询服务器返回的构筑生成（玩家详情 / 卡组详情页的「小世界」「联立炮」按钮）。 */
        fun fromBuild(label: String, build: BuildData): ToolDeck {
            fun expand(partition: String) = build.cards
                .filter { it.partition == partition && it.code > 0 && it.copies > 0 }
                .flatMap { c -> List(c.copies) { c.code } }
            return ToolDeck(label, expand("main"), expand("extra"))
        }

        /**
         * 解析 YDK 文本：`#main` / `#extra` / `!side` 三段，每行一个卡号。
         * 也接受没有分段标记的裸卡号清单（全部算主卡组）。解析不出任何卡号返回 null。
         */
        fun parseYdk(text: String, label: String = "导入的卡组"): ToolDeck? {
            val main = ArrayList<Long>()
            val extra = ArrayList<Long>()
            var section = "main"
            for (rawLine in text.lineSequence()) {
                val line = rawLine.trim()
                if (line.isEmpty()) continue
                when {
                    line.startsWith("#main", ignoreCase = true) -> section = "main"
                    line.startsWith("#extra", ignoreCase = true) -> section = "extra"
                    line.startsWith("!side", ignoreCase = true) -> section = "side"
                    line.startsWith("#") || line.startsWith("!") -> Unit // 注释，如 #created by
                    else -> {
                        val code = line.takeWhile { it.isDigit() }.toLongOrNull() ?: continue
                        if (code <= 0) continue
                        when (section) {
                            "main" -> main.add(code)
                            "extra" -> extra.add(code)
                        }
                    }
                }
            }
            if (main.isEmpty() && extra.isEmpty()) return null
            return ToolDeck(label, main, extra)
        }
    }
}

/** 页面间传递当前卡组：构筑页点按钮后带过去，两个计算器共用。 */
object ToolDeckStore {
    private val _deck = MutableStateFlow<ToolDeck?>(null)
    val deck: StateFlow<ToolDeck?> = _deck.asStateFlow()

    fun set(deck: ToolDeck?) {
        _deck.value = deck
    }
}

/** 两个计算器。 */
enum class ToolKind { SMALL_WORLD, CANNON }
