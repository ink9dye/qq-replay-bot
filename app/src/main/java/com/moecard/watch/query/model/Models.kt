package com.moecard.watch.query.model

import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

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
    // 线上 /query-api/card/{code} 的 details 是 {code,name,description,base_partition,...}，
    // 而查卡源（ygocdb）是 {cn_name,text:{name,desc,types,image_url}}；两种都兼容。
    return CardDetail(
        code = code,
        name = details?.optText("cn_name") ?: details?.optText("name") ?: text?.optText("name"),
        types = text?.optText("types") ?: details?.optText("types"),
        desc = details?.optText("description") ?: details?.optText("desc") ?: text?.optText("desc"),
        pdesc = text?.optText("pdesc") ?: details?.optText("pdesc"),
        imageUrl = details?.optText("image_url") ?: text?.optText("image_url"),
        details = details,
        metrics = metrics,
    )
}

// ---------- 绑定 / 换备 / 卡牌统计（2026-11-12 新增） ----------
//
// 契约见 服务端API对接.md：binding、player/{name}/side-plan、card/{code}/(usage|flow|winrate)。
// 全部防御解析：字段缺失给默认值，不因为一个 null 把整页打挂。

/** JSON null 与字符串 "null" 都当成缺失（org.json 的 optString 会把 null 读成 "null"）。 */
internal fun JSONObject.optText(key: String): String? {
    if (isNull(key)) return null
    val v = optString(key, "").trim()
    return if (v.isBlank() || v == "null") null else v
}

/** GET /query-api/binding：未绑定返回 player = null。 */
data class BindingInfo(val player: String?)

fun parseBinding(payload: Any?): BindingInfo {
    val root = payload as? JSONObject
    // org.json 的 optString 会把 JSON null 变成字符串 "null"，必须显式判 isNull
    val player = if (root == null || root.isNull("player")) "" else root.optString("player", "").trim()
    return BindingInfo(if (player.isBlank() || player == "null") null else player)
}

/** POST /query-api/binding：known = 最近 14 天收录里是否有这个名字。 */
data class BindResult(val player: String, val known: Boolean, val changed: Boolean)

fun parseBindResult(payload: Any?): BindResult {
    val root = payload as? JSONObject ?: JSONObject()
    return BindResult(
        player = root.optString("player", "").trim(),
        known = root.optBoolean("known", true),
        changed = root.optBoolean("changed", false),
    )
}

// ---------- 换备（Side Plan） ----------

/** 一张被换入/换出的卡。minCopies~maxCopies 是张数区间，与 QQ「换备」文案一致。 */
data class SideCard(
    val code: Long,
    val name: String,
    val partition: String,
    val sessions: Int,
    val sideGames: Int,
    val minCopies: Int,
    val maxCopies: Int,
) {
    val copiesLabel: String
        get() = if (minCopies == maxCopies) "$minCopies 张" else "$minCopies~$maxCopies 张"
}

/** 先攻 / 后攻一侧的汇总。 */
data class SideSeat(
    val sideGames: Int,
    val added: List<SideCard>,
    val removed: List<SideCard>,
)

/** 单场换边明细（原实现 _session_row 的字段子集）。 */
data class SideSession(
    val id: String,
    val recordedAt: Double,
    val opponent: String?,
    val gameCount: Int,
    val sideGames: Int,
    val added: List<SideCard>,
    val removed: List<SideCard>,
)

data class SideOpponent(
    val deck: String,
    val matches: Int,
    val recentMatches: Int,
    val sideGames: Int,
    val seatUnknownGames: Int,
    val sampleLow: Boolean,
    val first: SideSeat,
    val second: SideSeat,
    val sessions: List<SideSession>,
)

data class SidePlan(
    val player: String,
    val deck: String?,
    val totalMatches: Int,
    val opponents: List<SideOpponent>,
    val opponentRange: Int?,
    val excludedMatches: Int,
    val unverifiedMatches: Int,
    val notes: List<String>,
    val generatedAt: Double,
)

private fun parseSideCards(arr: JSONArray?): List<SideCard> {
    if (arr == null) return emptyList()
    val out = ArrayList<SideCard>(arr.length())
    for (i in 0 until arr.length()) {
        val o = arr.optJSONObject(i) ?: continue
        val code = o.optLong("code", 0L)
        if (code <= 0L) continue
        val minCopies = o.optInt("min_copies", 0)
        out.add(
            SideCard(
                code = code,
                name = o.optText("name") ?: "#$code",
                partition = o.optString("partition", "main"),
                sessions = o.optInt("sessions", 0),
                sideGames = o.optInt("side_games", 0),
                minCopies = minCopies,
                maxCopies = o.optInt("max_copies", minCopies),
            )
        )
    }
    return out
}

private fun parseSideSeat(o: JSONObject?): SideSeat = SideSeat(
    sideGames = o?.optInt("side_games", 0) ?: 0,
    added = parseSideCards(o?.optJSONArray("added")),
    removed = parseSideCards(o?.optJSONArray("removed")),
)

fun parseSidePlan(payload: Any?): SidePlan {
    val root = payload as? JSONObject ?: JSONObject()
    val opponents = ArrayList<SideOpponent>()
    val arr = root.optJSONArray("opponents")
    if (arr != null) {
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val sessions = ArrayList<SideSession>()
            val sarr = o.optJSONArray("sessions")
            if (sarr != null) {
                for (j in 0 until sarr.length()) {
                    val s = sarr.optJSONObject(j) ?: continue
                    sessions.add(
                        SideSession(
                            id = s.optString("id", ""),
                            recordedAt = s.optDouble("recorded_at", 0.0),
                            opponent = s.optText("opponent"),
                            gameCount = s.optInt("game_count", 0),
                            sideGames = s.optInt("side_games", 0),
                            added = parseSideCards(s.optJSONArray("added")),
                            removed = parseSideCards(s.optJSONArray("removed")),
                        )
                    )
                }
            }
            opponents.add(
                SideOpponent(
                    deck = o.optText("deck") ?: "?",
                    matches = o.optInt("matches", 0),
                    recentMatches = o.optInt("recent_matches", 0),
                    sideGames = o.optInt("side_games", 0),
                    seatUnknownGames = o.optInt("seat_unknown_games", 0),
                    sampleLow = o.optBoolean("sample_low", false),
                    first = parseSideSeat(o.optJSONObject("first")),
                    second = parseSideSeat(o.optJSONObject("second")),
                    sessions = sessions,
                )
            )
        }
    }
    val scope = root.optJSONObject("rank_scope")
    val notes = ArrayList<String>()
    val narr = root.optJSONArray("notes")
    if (narr != null) {
        for (i in 0 until narr.length()) {
            val t = narr.optString(i, "")
            if (t.isNotBlank()) notes.add(t)
        }
    }
    return SidePlan(
        player = root.optText("player") ?: "",
        deck = root.optText("deck"),
        totalMatches = root.optInt("total_matches", 0),
        opponents = opponents,
        opponentRange = if (scope != null && scope.has("opponent_range") && !scope.isNull("opponent_range")) {
            scope.optInt("opponent_range", 0)
        } else null,
        excludedMatches = scope?.optInt("excluded_matches", 0) ?: 0,
        unverifiedMatches = scope?.optInt("unverified_matches", 0) ?: 0,
        notes = notes,
        generatedAt = root.optDouble("generated_at", 0.0),
    )
}

// ---------- 卡牌三统计 ----------

/** 0/1/2/3+ 张的人数分布（3 表示 3 张及以上，与 QQ 饼图口径一致）。 */
data class CountDist(val buckets: List<Pair<Int, Int>>) {
    fun countOf(n: Int): Int = buckets.firstOrNull { it.first == n }?.second ?: 0
    val total: Int get() = buckets.sumOf { it.second }
}

private fun parseDist(o: JSONObject?): CountDist {
    if (o == null) return CountDist(emptyList())
    val list = ArrayList<Pair<Int, Int>>()
    // 固定 0/1/2/3 四档；缺哪档补 0，保证 UI 不会因为缺 key 少画一列
    for (n in 0..3) {
        list.add(n to o.optInt(n.toString(), 0))
    }
    return CountDist(list)
}

data class UsageRecord(
    val player: String,
    val rank: Int,
    val main: Int,
    val side: Int,
    val total: Int,
    val mainKnown: Boolean,
    val sideKnown: Boolean,
    val hasBuild: Boolean,
)

data class CardUsage(
    val code: Long,
    val name: String,
    val covered: Int,
    val current: Int,
    val excluded: Int,
    val totalPlayers: Int,
    val mainDist: CountDist,
    val sideDist: CountDist,
    val totalDist: CountDist,
    val mainTotal: Int,
    val sideTotal: Int,
    val totalTotal: Int,
    val sideUsed: Int,
    val records: List<UsageRecord>,
    val recordsTotal: Int,
    val truncated: Boolean,
    val generatedAt: Double,
)

fun parseCardUsage(payload: Any?): CardUsage {
    val root = payload as? JSONObject ?: JSONObject()
    val records = ArrayList<UsageRecord>()
    val arr = root.optJSONArray("records")
    if (arr != null) {
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            records.add(
                UsageRecord(
                    player = o.optString("player", ""),
                    rank = o.optInt("rank", 0),
                    main = o.optInt("main", 0),
                    side = o.optInt("side", 0),
                    total = o.optInt("total", 0),
                    mainKnown = o.optBoolean("main_known", false),
                    // 服务端历史字段名是 known（= side_known）；两种都认
                    sideKnown = o.optBoolean("side_known", o.optBoolean("known", false)),
                    hasBuild = o.optBoolean("has_build", false),
                )
            )
        }
    }
    return CardUsage(
        code = root.optLong("code", 0L),
        name = root.optText("name") ?: "",
        covered = root.optInt("covered", 0),
        current = root.optInt("current", 0),
        excluded = root.optInt("excluded", 0),
        totalPlayers = root.optInt("total_players", 0),
        mainDist = parseDist(root.optJSONObject("main_dist")),
        sideDist = parseDist(root.optJSONObject("side_dist")),
        totalDist = parseDist(root.optJSONObject("total_dist")),
        mainTotal = root.optInt("main_total", 0),
        sideTotal = root.optInt("side_total", 0),
        totalTotal = root.optInt("total_total", 0),
        sideUsed = root.optInt("side_used", 0),
        records = records,
        recordsTotal = root.optInt("records_total", records.size),
        truncated = root.optBoolean("truncated", false),
        generatedAt = root.optDouble("generated_at", 0.0),
    )
}

/** 单卡流向里某一个卡组的 main/side/total 桶；rate 为 0~1，无分母为 null。 */
data class FlowBucket(
    val known: Int,
    val used: Int,
    val one: Int,
    val two: Int,
    val three: Int,
    val rate: Double?,
) {
    val percentText: String
        get() = rate?.let { String.format(Locale.ROOT, "%.0f%%", it * 100) } ?: "—"
}

private fun parseFlowBucket(o: JSONObject?): FlowBucket = FlowBucket(
    known = o?.optInt("known", 0) ?: 0,
    used = o?.optInt("used", 0) ?: 0,
    one = o?.optInt("one", 0) ?: 0,
    two = o?.optInt("two", 0) ?: 0,
    three = o?.optInt("three", 0) ?: 0,
    rate = if (o == null || o.isNull("rate")) null else o.optDouble("rate", 0.0),
)

data class FlowDeck(
    val deck: String,
    val players: Int,
    val main: FlowBucket,
    val side: FlowBucket,
    val total: FlowBucket,
)

data class CardFlow(
    val code: Long,
    val name: String,
    val covered: Int,
    val current: Int,
    val totalPlayers: Int,
    val mainUsed: Int,
    val sideUsed: Int,
    val totalUsed: Int,
    val items: List<FlowDeck>,
    val generatedAt: Double,
)

fun parseCardFlow(payload: Any?): CardFlow {
    val root = payload as? JSONObject ?: JSONObject()
    val items = ArrayList<FlowDeck>()
    // 优先 items（全量卡组）；老服务端只给 bars 时退化成 bars
    val arr = root.optJSONArray("items") ?: root.optJSONObject("bars")?.optJSONArray("total")
    if (arr != null) {
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val deck = o.optText("deck") ?: "?"
            items.add(
                FlowDeck(
                    deck = deck,
                    players = o.optInt("players", 0),
                    main = parseFlowBucket(o.optJSONObject("main")),
                    side = parseFlowBucket(o.optJSONObject("side")),
                    total = parseFlowBucket(o.optJSONObject("total")),
                )
            )
        }
    }
    return CardFlow(
        code = root.optLong("code", 0L),
        name = root.optText("name") ?: "",
        covered = root.optInt("covered", 0),
        current = root.optInt("current", 0),
        totalPlayers = root.optInt("total_players", 0),
        mainUsed = root.optInt("main_used", 0),
        sideUsed = root.optInt("side_used", 0),
        totalUsed = root.optInt("total_used", 0),
        items = items,
        generatedAt = root.optDouble("generated_at", 0.0),
    )
}

/** 胜率分母：games = wins + losses + draws，rate 为 0~100，无分母为 null。 */
data class RateBase(
    val games: Int,
    val wins: Int,
    val losses: Int,
    val draws: Int,
    val rate: Double?,
) {
    val rateText: String
        get() = rate?.let { String.format(Locale.ROOT, "%.1f%%", it) } ?: "—"
}

private fun parseRateBase(o: JSONObject?): RateBase? {
    if (o == null) return null
    if (!o.has("games") && !o.has("wins")) return null
    return RateBase(
        games = o.optInt("games", 0),
        wins = o.optInt("wins", 0),
        losses = o.optInt("losses", 0),
        draws = o.optInt("draws", 0),
        rate = if (o.isNull("rate")) null else o.optDouble("rate", 0.0),
    )
}

data class MatchupRow(
    val deck: String,
    val games: Int,
    val wins: Int,
    val rate: Double?,
    val lowSample: Boolean,
) {
    val rateText: String
        get() = rate?.let { String.format(Locale.ROOT, "%.1f%%", it) } ?: "—"
}

data class CardWinrate(
    val code: Long,
    val name: String,
    val base: RateBase?,
    val rows: List<MatchupRow>,
    val totalDecks: Int,
    val qualifiedDecks: Int,
    val notes: List<String>,
    val first: RateBase?,
    val second: RateBase?,
    val unknown: RateBase?,
    val generatedAt: Double,
)

fun parseCardWinrate(payload: Any?): CardWinrate {
    val root = payload as? JSONObject ?: JSONObject()
    val matchups = root.optJSONObject("matchups")
    val rows = ArrayList<MatchupRow>()
    val arr = matchups?.optJSONArray("rows")
    if (arr != null) {
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            rows.add(
                MatchupRow(
                    deck = o.optText("deck_type") ?: o.optText("deck") ?: "?",
                    games = o.optInt("games", 0),
                    wins = o.optInt("wins", 0),
                    rate = if (o.isNull("rate")) null else o.optDouble("rate", 0.0),
                    lowSample = o.optBoolean("low_sample", false),
                )
            )
        }
    }
    val notes = ArrayList<String>()
    val narr = matchups?.optJSONArray("notes")
    if (narr != null) {
        for (i in 0 until narr.length()) {
            val t = narr.optString(i, "")
            if (t.isNotBlank()) notes.add(t)
        }
    }
    val order = root.optJSONObject("order")
    return CardWinrate(
        code = root.optLong("code", 0L),
        name = root.optText("name") ?: "",
        base = parseRateBase(matchups?.optJSONObject("base")),
        rows = rows,
        totalDecks = matchups?.optInt("total_decks", 0) ?: 0,
        qualifiedDecks = matchups?.optInt("qualified_decks", 0) ?: 0,
        notes = notes,
        first = parseRateBase(order?.optJSONObject("first")),
        second = parseRateBase(order?.optJSONObject("second")),
        unknown = parseRateBase(order?.optJSONObject("unknown")),
        generatedAt = root.optDouble("generated_at", 0.0),
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
