package com.moecard.watch.query.model

import org.json.JSONArray
import org.json.JSONObject

/**
 * 查询端数据模型。两个数据源：
 *  - 萌卡官方 arena 接口（sapi.moecube.com:444）：玩家资料 / 对战历史 / DP 曲线
 *  - 自建查询 API（/query-api 前缀）：前百 / 构筑 / 录像 / 订阅 / 饼图 / 搜索 / 卡片
 *
 * 服务端部分字段是门户透传、形状以源码为准，解析一律走 opt* 防御式：
 * 字段缺失给默认值，不认识的原始 JSON 留在 raw 里供兜底渲染（JsonViews）。
 */

// ---------- 会话 / 令牌 ----------

data class TokenPair(
    val token: String,
    val refreshToken: String,
    val accessExpiresAt: Long,
    val refreshExpiresAt: Long,
    val identity: String,
    val role: String,
    val writes: Boolean,
)

data class QueryIdentity(
    val identity: String,
    val role: String,
    val writes: Boolean,
    val device: String? = null,
)

fun parseTokenPair(json: JSONObject): TokenPair? {
    val token = json.optString("token", "")
    val refresh = json.optString("refresh_token", "")
    if (token.isBlank() || refresh.isBlank()) return null
    return TokenPair(
        token = token,
        refreshToken = refresh,
        accessExpiresAt = json.optLong("access_expires_at", 0L),
        refreshExpiresAt = json.optLong("refresh_expires_at", 0L),
        identity = json.optString("identity", ""),
        role = json.optString("role", "user"),
        writes = json.optBoolean("writes", false),
    )
}

fun parseIdentity(json: JSONObject): QueryIdentity = QueryIdentity(
    identity = json.optString("identity", ""),
    role = json.optString("role", "user"),
    writes = json.optBoolean("writes", false),
    device = if (json.has("device")) json.optString("device") else null,
)

// ---------- 官方 arena：玩家资料 / 对战历史 ----------

/** 官方 /ygopro/arena/user 结果。pt 即玩家口中的 DP（竞技场积分）。 */
data class ArenaProfile(
    val pt: Double,
    val arenaRank: Int,
    val athleticAll: Int,
    val athleticWin: Int,
    val athleticLose: Int,
    val athleticDraw: Int,
    val wlRatio: String,
    val exp: Double,
    val expRank: Int,
    val entertainAll: Int,
    val entertainWin: Int,
    val raw: JSONObject,
) {
    val winRate: Int
        get() = if (athleticAll > 0) (athleticWin * 100.0 / athleticAll + 0.5).toInt() else 0
}

/**
 * 官方 /ygopro/arena/history 的单行。官方响应可能是 dict 也可能是单元素 list
 * （服务端 arena_profile.py 两种都处理过），这里同样兼容。
 */
fun parseArenaProfile(payload: Any?): ArenaProfile? {
    val o: JSONObject = when (payload) {
        is JSONObject -> payload
        is JSONArray -> payload.optJSONObject(0) ?: return null
        else -> return null
    }
    val pt = o.optDouble("pt", Double.NaN)
    if (pt.isNaN()) return null
    return ArenaProfile(
        pt = pt,
        arenaRank = o.optInt("arena_rank", 0),
        athleticAll = o.optInt("athletic_all", 0),
        athleticWin = o.optInt("athletic_win", 0),
        athleticLose = o.optInt("athletic_lose", 0),
        athleticDraw = o.optInt("athletic_draw", 0),
        wlRatio = o.optString("athletic_wl_ratio", "0"),
        exp = o.optDouble("exp", 0.0),
        expRank = o.optInt("exp_rank", 0),
        entertainAll = o.optInt("entertain_all", 0),
        entertainWin = o.optInt("entertain_win", 0),
        raw = o,
    )
}

/** 一场竞技对局。ptBefore/ptAfter 是本人一侧的赛前 / 赛后分，DP 曲线靠它们连线。 */
data class ArenaMatch(
    val opponent: String,
    val startTime: Long,
    val endTime: Long,
    val won: Boolean?, // null = 平局
    val ptBefore: Double,
    val ptAfter: Double,
)

/**
 * 解析官方 history 响应（{total, data:[...]}，可能合并自两页）。
 * 只保留 type == "athletic" 的行，按 (start_time, end_time) 去重后按开始时间升序。
 */
fun parseArenaHistory(payload: Any?, username: String): List<ArenaMatch> {
    val o: JSONObject = when (payload) {
        is JSONObject -> payload
        else -> return emptyList()
    }
    val data = o.optJSONArray("data") ?: return emptyList()
    val seen = HashSet<Long>()
    val out = ArrayList<ArenaMatch>()
    val me = username.trim()
    for (i in 0 until data.length()) {
        val row = data.optJSONObject(i) ?: continue
        if (row.optString("type", "") != "athletic") continue
        val a = row.optString("usernamea", "")
        val b = row.optString("usernameb", "")
        val isSideA = a == me
        if (!isSideA && b != me) continue // 不在本局双方里，跳过
        // 官方现网是 ISO-8601 字符串（"2026-11-06T09:08:40.000Z"），optLong 会得到 0，
        // 所有行挤成同一个去重键、只剩一条——这就是「对战历史只能加载一局」的原因。
        val start = parseArenaTime(row.opt("start_time"))
        if (start <= 0) continue
        val end = parseArenaTime(row.opt("end_time")).takeIf { it > 0 } ?: start
        if (!seen.add(start * 1000_003L + end)) continue
        val winner = row.optString("winner", "")
        val won: Boolean? = when {
            winner == me -> true
            winner.isBlank() || winner == "none" -> null
            else -> false
        }
        val (before, after) = if (isSideA) {
            row.optDouble("pta_ex", Double.NaN) to row.optDouble("pta", Double.NaN)
        } else {
            row.optDouble("ptb_ex", Double.NaN) to row.optDouble("ptb", Double.NaN)
        }
        out.add(
            ArenaMatch(
                opponent = if (isSideA) b else a,
                startTime = start,
                endTime = end,
                won = won,
                ptBefore = if (before.isNaN()) after else before,
                ptAfter = if (after.isNaN()) before else after,
            )
        )
    }
    out.sortBy { it.startTime }
    return out
}

/**
 * 官方 history 的时间字段：现网是 ISO-8601（"2026-11-06T09:08:40.000Z"），
 * 老数据/测试里也可能是 epoch 秒。解析不了返回 0。
 */
private fun parseArenaTime(v: Any?): Long = when (v) {
    is Number -> v.toLong()
    is String -> v.trim().let { s ->
        s.toLongOrNull() ?: runCatching { java.time.Instant.parse(s).epochSecond }
            .recoverCatching { java.time.OffsetDateTime.parse(s).toInstant().epochSecond }
            .getOrDefault(0L)
    }
    else -> 0L
}

/** DP 曲线：首局赛前分 + 每局赛后分（对齐服务端 player_history.py 的算法）。 */
fun dpCurve(matches: List<ArenaMatch>): List<Double> {
    if (matches.isEmpty()) return emptyList()
    return listOf(matches.first().ptBefore) + matches.map { it.ptAfter }
}

// ---------- 自建查询 API：前百 / 构筑 / 录像 / 饼图 / 搜索 / 卡片 / 订阅 ----------

/** 前百成员（门户 roster 的 person）。 */
data class RosterPerson(
    val player: String,
    val rank: Int,
    val points: Double,
    val observed: Boolean,
    val deckType: String?,
    /** 服务端是数组（如 ["三幻魔","混沌",…]），老版本可能是逗号分隔字符串。 */
    val series: List<String>,
    val raw: JSONObject,
)

data class DeckDistItem(val name: String, val players: Int)

/** top100 返回：门户 roster 快照（people + 分布 + 覆盖情况）。 */
data class RosterData(
    val people: List<RosterPerson>,
    val deckDistribution: List<DeckDistItem>,
    val covered: Int,
    val current: Int,
    val unidentified: Int,
)

/** 单个 person 对象 → RosterPerson（roster 行 / build.person 共用）。 */
private fun parsePerson(o: JSONObject): RosterPerson? {
    val name = o.optString("player", "")
    if (name.isBlank()) return null
    return RosterPerson(
        player = name,
        rank = o.optInt("rank", 0),
        points = o.optDouble("points", 0.0),
        observed = o.optBoolean("observed", false),
        deckType = o.optString("deck_type").takeIf { it.isNotBlank() },
        series = parseSeries(o.opt("series")),
        raw = o,
    )
}

/**
 * series 兼容三种形状：数组（现行服务端）、逗号/顿号分隔字符串（老版本）、
 * 缺省。只做展示与卡组缩略图匹配用，解析失败不致命。
 */
private fun parseSeries(value: Any?): List<String> = when (value) {
    is JSONArray -> (0 until value.length()).mapNotNull { i ->
        value.optString(i, "").trim().takeIf { it.isNotBlank() }
    }
    is String -> value.split(',', '，', '、').map { it.trim() }.filter { it.isNotBlank() }
    else -> emptyList()
}

fun parseRoster(payload: Any?): RosterData {
    // data 可能是 {people:[...], deck_distribution:[...], ...} 快照，也可能直接是列表
    val peopleArr: JSONArray? = when (payload) {
        is JSONObject -> payload.optJSONArray("people")
        is JSONArray -> payload
        else -> null
    }
    val root = if (payload is JSONObject) payload else JSONObject()
    val people = ArrayList<RosterPerson>()
    val seenPlayers = HashSet<String>()
    if (peopleArr != null) {
        for (i in 0 until peopleArr.length()) {
            val o = peopleArr.optJSONObject(i) ?: continue
            val p = parsePerson(o) ?: continue
            if (!seenPlayers.add(p.player)) continue // 列表 key 需唯一
            people.add(p)
        }
    }
    people.sortBy { it.rank }
    val dist = ArrayList<DeckDistItem>()
    val distArr = root.optJSONArray("deck_distribution")
    if (distArr != null) {
        for (i in 0 until distArr.length()) {
            val o = distArr.optJSONObject(i) ?: continue
            dist.add(DeckDistItem(o.optString("name", "?"), o.optInt("players", 0)))
        }
    }
    return RosterData(
        people = people,
        deckDistribution = dist,
        covered = root.optInt("covered_players", 0),
        current = root.optInt("current_players", 0),
        unidentified = root.optInt("unidentified_players", 0),
    )
}

private fun parsePeopleList(payload: Any?): List<RosterPerson> =
    parseRoster(payload).people

// ---------- 构筑 ----------

data class BuildCard(
    val partition: String,
    val code: Long,
    val copies: Int,
    /** 服务端随卡号返回的卡名；老版本服务端可能没有。 */
    val name: String? = null,
)

data class BuildData(
    val person: RosterPerson?,
    val cards: List<BuildCard>,
    val mainComplete: Boolean,
    val extraComplete: Boolean,
    val sideComplete: Boolean,
)

fun parseBuild(payload: Any?): BuildData? {
    val root = if (payload is JSONObject) payload else return null
    val cards = ArrayList<BuildCard>()
    val arr = root.optJSONArray("cards")
    if (arr != null) {
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            cards.add(
                BuildCard(
                    partition = o.optString("partition", ""),
                    code = o.optLong("code", 0L),
                    copies = o.optInt("copies", 0),
                    name = o.optString("name").takeIf { it.isNotBlank() },
                )
            )
        }
    }
    val personObj = root.optJSONObject("person")
    val person = personObj?.let { parsePerson(it) }
    val comp = root.optJSONObject("completeness")
    return BuildData(
        person = person,
        cards = cards,
        mainComplete = comp?.optBoolean("main", false) ?: false,
        extraComplete = comp?.optBoolean("extra", false) ?: false,
        sideComplete = comp?.optBoolean("side", false) ?: false,
    )
}

// ---------- 录像 ----------

data class ReplayItem(
    val id: String,
    val name: String,
    val recordedAt: Double,
    val url: String,
)

data class ReplayLink(
    val url: String,
    val expiresAt: Long?,
    val isPublic: Boolean?,
)

data class ReplayData(
    val items: List<ReplayItem>,
    val link: ReplayLink?,
)

fun parseReplays(payload: Any?): ReplayData {
    val root = if (payload is JSONObject) payload else return ReplayData(emptyList(), null)
    val items = ArrayList<ReplayItem>()
    val arr = root.optJSONArray("items")
    if (arr != null) {
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            items.add(
                ReplayItem(
                    id = o.optString("id"),
                    name = o.optString("name", o.optString("id")),
                    recordedAt = o.optDouble("recorded_at", 0.0),
                    url = o.optString("url", ""),
                )
            )
        }
    }
    val linkObj = root.optJSONObject("link")
    val link = if (linkObj != null && linkObj.optString("url").isNotBlank()) {
        ReplayLink(
            url = linkObj.optString("url"),
            expiresAt = if (linkObj.has("expires_at")) linkObj.optLong("expires_at") else null,
            isPublic = if (linkObj.has("public")) linkObj.optBoolean("public") else null,
        )
    } else null
    return ReplayData(items, link)
}

// ---------- 饼图 ----------

data class PieSlice(val name: String, val count: Int)

/** 内环：主大类。image/bandImage 是服务端选好的代表卡（组内代表卡的卡号）。 */
data class PieCategory(
    val name: String,
    val players: Int,
    val percent: Double,
    val color: String,
    val image: Long?,
    val bandImage: Long?,
)

/** 中环：主大类交集细分（独占 / ∩其他大类），只用于图例，不单独成环。 */
data class PieMiddle(
    val primary: String,
    val label: String,
    val extras: List<String>,
    val players: Int,
    val percent: Double,
    val color: String,
    val accent: String?,
    val image: Long?,
    val mixed: String?,
)

/** 外环：精确卡组名。color 是同色系按排名明度递进的色值，image 是该卡组的代表卡。 */
data class PieDeck(
    val name: String,
    val players: Int,
    val percent: Double,
    val primary: String,
    val extras: List<String>,
    val shared: Boolean,
    val color: String,
    val accent: String?,
    val image: Long?,
)

/**
 * 服务端 `pie` 三层数据（bot/deck_pie.py 的输出，口径与统计网页一致）：
 * 内环主大类合计 100%，中环是交集细分，外环为具体卡组名合计 100%。
 */
data class PieLayers(
    val total: Int,
    val threshold: Int,
    val inner: List<PieCategory>,
    val middle: List<PieMiddle>,
    val outer: List<PieDeck>,
    val innerPercent: Double,
    val middlePercent: Double,
    val outerPercent: Double,
    val qualified: List<String>,
    val note: String,
)

data class PieData(
    val items: List<PieSlice>,
    val covered: Int,
    val current: Int,
    val unidentified: Int,
    val pie: PieLayers? = null,
)

private fun JSONObject.optImage(key: String): Long? =
    optLong(key, 0L).takeIf { it > 0 }

private fun JSONObject.optStrings(key: String): List<String> {
    val arr = optJSONArray(key) ?: return emptyList()
    return (0 until arr.length()).mapNotNull { arr.optString(it, "").takeIf { s -> s.isNotBlank() } }
}

private fun parsePieLayers(o: JSONObject?): PieLayers? {
    if (o == null) return null
    val total = o.optInt("total", 0)
    if (total <= 0) return null
    val inner = ArrayList<PieCategory>()
    o.optJSONArray("inner")?.let { arr ->
        for (i in 0 until arr.length()) {
            val row = arr.optJSONObject(i) ?: continue
            inner.add(
                PieCategory(
                    name = row.optString("name", "?"),
                    players = row.optInt("players", 0),
                    percent = row.optDouble("percent", 0.0),
                    color = row.optString("color", "#C9D2DA"),
                    image = row.optImage("image"),
                    bandImage = row.optImage("band_image"),
                )
            )
        }
    }
    val middle = ArrayList<PieMiddle>()
    o.optJSONArray("middle")?.let { arr ->
        for (i in 0 until arr.length()) {
            val row = arr.optJSONObject(i) ?: continue
            middle.add(
                PieMiddle(
                    primary = row.optString("primary", ""),
                    label = row.optString("label", ""),
                    extras = row.optStrings("extras"),
                    players = row.optInt("players", 0),
                    percent = row.optDouble("percent", 0.0),
                    color = row.optString("color", "#C9D2DA"),
                    accent = row.optString("accent", "").takeIf { it.isNotBlank() },
                    image = row.optImage("image"),
                    mixed = row.optString("mixed", "").takeIf { it.isNotBlank() },
                )
            )
        }
    }
    val outer = ArrayList<PieDeck>()
    o.optJSONArray("outer")?.let { arr ->
        for (i in 0 until arr.length()) {
            val row = arr.optJSONObject(i) ?: continue
            outer.add(
                PieDeck(
                    name = row.optString("name", "?"),
                    players = row.optInt("players", 0),
                    percent = row.optDouble("percent", 0.0),
                    primary = row.optString("primary", "其他"),
                    extras = row.optStrings("extras"),
                    shared = row.optBoolean("shared", false),
                    color = row.optString("color", "#8C9FB0"),
                    accent = row.optString("accent", "").takeIf { it.isNotBlank() },
                    image = row.optImage("image"),
                )
            )
        }
    }
    return PieLayers(
        total = total,
        threshold = o.optInt("threshold", 3),
        inner = inner,
        middle = middle,
        outer = outer,
        innerPercent = o.optDouble("inner_percent", 0.0),
        middlePercent = o.optDouble("middle_percent", 0.0),
        outerPercent = o.optDouble("outer_percent", 0.0),
        qualified = o.optStrings("qualified"),
        note = o.optString("note", ""),
    )
}

fun parsePie(payload: Any?): PieData {
    val root = if (payload is JSONObject) payload else return PieData(emptyList(), 0, 0, 0, null)
    val items = ArrayList<PieSlice>()
    val arr = root.optJSONArray("items")
    if (arr != null) {
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            items.add(PieSlice(o.optString("name", "?"), o.optInt("count", 0)))
        }
    }
    return PieData(
        items = items,
        covered = root.optInt("covered", 0),
        current = root.optInt("current", 0),
        unidentified = root.optInt("unidentified", 0),
        pie = parsePieLayers(root.optJSONObject("pie")),
    )
}

// ---------- 搜索 ----------

data class CardSearchHit(
    val id: Long,
    val name: String,
    val types: String?,
)

data class SearchResults(
    val players: List<RosterPerson>,
    val cards: List<CardSearchHit>,
    val decks: List<DeckDistItem>,
    val limited: Boolean,
)

/** search?type=player|card|deck。三种结果形状不同，分别防御解析。 */
fun parseSearch(type: String, payload: Any?): SearchResults {
    val root = if (payload is JSONObject) payload else JSONObject()
    val limited = root.optBoolean("limited", false)
    return when (type) {
        "player" -> SearchResults(parsePeopleList(payload), emptyList(), emptyList(), limited)
        "deck" -> {
            val decks = ArrayList<DeckDistItem>()
            val arr = root.optJSONArray("items")
            if (arr != null) {
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    decks.add(DeckDistItem(o.optString("name", "?"), o.optInt("players", 0)))
                }
            }
            SearchResults(emptyList(), emptyList(), decks, limited)
        }
        else -> {
            // 卡片搜索是门户透传：服务端返回 {cards:[{code, name}]}；
            // 兼容 ygocdb 的 {result:[{id, cn_name, text:{types}}]} 形状。
            val cards = ArrayList<CardSearchHit>()
            val arr = root.optJSONArray("cards")
                ?: root.optJSONArray("result")
                ?: root.optJSONArray("items")
                ?: root.optJSONArray("data")
            if (arr != null) {
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    val id = o.optLong("code", o.optLong("id", 0L))
                    if (id <= 0) continue
                    val text = o.optJSONObject("text")
                    val name = o.optString("name", "")
                        .ifBlank { o.optString("cn_name", id.toString()) }
                    cards.add(
                        CardSearchHit(
                            id = id,
                            name = name,
                            types = (o.optString("types", "").ifBlank { text?.optString("types") })
                                ?.takeIf { it.isNotBlank() },
                        )
                    )
                }
            }
            SearchResults(emptyList(), cards, emptyList(), limited)
        }
    }
}

// ---------- 卡片详情 ----------

data class CardDetail(
    val code: Long,
    val name: String?,
    val types: String?,
    val desc: String?,
    val pdesc: String?,
    val imageUrl: String?,
    val details: JSONObject?, // 原始 details，兜底渲染
    val metrics: JSONObject?, // 原始 metrics（可为 null），字段结构见 JsonViews 的 rate 渲染
)

fun parseCard(code: Long, detailsPayload: Any?, metricsPayload: Any?): CardDetail {
    val details = if (detailsPayload is JSONObject) detailsPayload else null
    val text = details?.optJSONObject("text")
    val metrics = if (metricsPayload is JSONObject) metricsPayload else null

    // 现行服务端 /query-api/card/<code> 返回 details.{code,name,description,…}；
    // 老版本/门户透传是 details.text.{name,desc,types}。两种都兼容。
    fun str(obj: JSONObject?, key: String): String? =
        obj?.optString(key)?.takeIf { it.isNotBlank() }

    return CardDetail(
        code = code,
        name = str(details, "cn_name") ?: str(text, "name") ?: str(details, "name"),
        types = str(text, "types") ?: str(details, "types"),
        desc = str(text, "desc") ?: str(details, "description") ?: str(details, "desc"),
        pdesc = str(text, "pdesc") ?: str(details, "pdesc"),
        imageUrl = str(details, "image_url") ?: str(text, "image_url"),
        details = details,
        metrics = metrics,
    )
}

// ---------- 订阅 ----------

data class Subscription(
    val player: String,
    val rankTop: Int?,
    val notify: Boolean,
)

fun parseSubscriptions(payload: Any?): List<Subscription> {
    val root = if (payload is JSONObject) payload else return emptyList()
    val arr = root.optJSONArray("items") ?: return emptyList()
    val out = ArrayList<Subscription>()
    for (i in 0 until arr.length()) {
        val o = arr.optJSONObject(i) ?: continue
        val player = o.optString("player", "")
        if (player.isBlank()) continue
        out.add(
            Subscription(
                player = player,
                rankTop = if (o.has("rank_top") && !o.isNull("rank_top")) o.optInt("rank_top") else null,
                notify = o.optBoolean("notify", false),
            )
        )
    }
    return out
}

// ---------- 录像库 /replay-api（v2 按对局检索） ----------

data class ReplayView(
    val player: String,
    val outcome: String, // win / loss / draw
    val deck: String,
)

data class ReplayGame(
    val id: Long,
    val gameNo: Int,
    val name: String,
    val size: Long,
    val views: List<ReplayView>,
    val download: String,
)

/** 一场对局 = 同 session 的 1~3 局录像。 */
data class ReplayMatch(
    val key: String,
    val session: String,
    val players: List<String>,
    val decks: Map<String, String>,
    val score: String,
    val winner: String,
    val decided: Int,
    val start: Long,
    val end: Long,
    val gameCount: Int,
    val bytes: Long,
    val available: Boolean,
    val folder: String,
    val download: String,
    val games: List<ReplayGame>,
)

data class ReplaySearch(
    val page: Int,
    val pageSize: Int,
    val total: Int,
    val totalBytes: Long,
    val items: List<ReplayMatch>,
)

fun parseReplaySearch(root: JSONObject): ReplaySearch {
    val items = ArrayList<ReplayMatch>()
    val arr = root.optJSONArray("items")
    if (arr != null) {
        for (i in 0 until arr.length()) {
            val m = parseReplayMatch(arr.optJSONObject(i)) ?: continue
            items.add(m)
        }
    }
    return ReplaySearch(
        page = root.optInt("page", 1),
        pageSize = root.optInt("page_size", 30),
        total = root.optInt("total", 0),
        totalBytes = root.optLong("total_bytes", 0L),
        items = items,
    )
}

private fun parseReplayMatch(o: JSONObject?): ReplayMatch? {
    if (o == null) return null
    val key = o.optString("key", "")
    if (key.isBlank()) return null
    val players = ArrayList<String>()
    o.optJSONArray("players")?.let { a ->
        for (i in 0 until a.length()) {
            a.optString(i, "").trim().takeIf { it.isNotBlank() }?.let { players.add(it) }
        }
    }
    val decks = LinkedHashMap<String, String>()
    o.optJSONObject("decks")?.let { d ->
        d.keys().forEach { k -> decks[k] = d.optString(k, "") }
    }
    val games = ArrayList<ReplayGame>()
    o.optJSONArray("games")?.let { a ->
        for (i in 0 until a.length()) {
            val g = a.optJSONObject(i) ?: continue
            val id = g.optLong("id", 0L)
            if (id <= 0L) continue
            val views = ArrayList<ReplayView>()
            g.optJSONArray("views")?.let { vs ->
                for (j in 0 until vs.length()) {
                    val v = vs.optJSONObject(j) ?: continue
                    val p = v.optString("player", "").trim()
                    if (p.isBlank()) continue
                    views.add(
                        ReplayView(
                            player = p,
                            outcome = v.optString("outcome", ""),
                            deck = v.optString("deck", ""),
                        )
                    )
                }
            }
            games.add(
                ReplayGame(
                    id = id,
                    gameNo = g.optInt("game_no", i + 1),
                    name = g.optString("name", ""),
                    size = g.optLong("size", 0L),
                    views = views,
                    download = g.optString("download", "/replay-api/download?id=$id"),
                )
            )
        }
    }
    return ReplayMatch(
        key = key,
        session = o.optString("session", ""),
        players = players,
        decks = decks,
        score = o.optString("score", ""),
        winner = o.optString("winner", ""),
        decided = o.optInt("decided", 0),
        start = parseArenaTime(o.opt("start")),
        end = parseArenaTime(o.opt("end")),
        gameCount = o.optInt("game_count", games.size),
        bytes = o.optLong("bytes", 0L),
        available = o.optBoolean("available", true),
        folder = o.optString("folder", ""),
        download = o.optString("download", ""),
        games = games,
    )
}

/** 我方在这场的视角胜负：win / loss / draw，未知返回 null。 */
fun ReplayMatch.outcomeFor(player: String): String? {
    games.asSequence().flatMap { it.views.asSequence() }
        .firstOrNull { it.player == player }?.let { return it.outcome }
    return null
}

/** 整场胜负（winner 优先，未分胜负退回逐局视角）：win / loss / null。 */
fun ReplayMatch.resultFor(player: String): String? = when {
    winner.isNotBlank() -> if (winner == player) "win" else "loss"
    else -> outcomeFor(player)?.takeIf { it == "win" || it == "loss" }
}
