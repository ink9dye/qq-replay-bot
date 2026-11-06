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
        val start = row.optLong("start_time", 0L)
        val end = row.optLong("end_time", start)
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
    val series: String?,
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
        series = o.optString("series").takeIf { it.isNotBlank() },
        raw = o,
    )
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

data class BuildCard(val partition: String, val code: Long, val copies: Int)

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

data class PieData(
    val items: List<PieSlice>,
    val covered: Int,
    val current: Int,
    val unidentified: Int,
)

fun parsePie(payload: Any?): PieData {
    val root = if (payload is JSONObject) payload else return PieData(emptyList(), 0, 0, 0)
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
            // 卡片搜索是门户透传，按 ygocdb 形状解析：{result:[{id, cn_name, text:{types,...}}]}
            val cards = ArrayList<CardSearchHit>()
            val arr = root.optJSONArray("result") ?: root.optJSONArray("items") ?: root.optJSONArray("data")
            if (arr != null) {
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    val id = o.optLong("id", 0L)
                    if (id <= 0) continue
                    val text = o.optJSONObject("text")
                    cards.add(
                        CardSearchHit(
                            id = id,
                            name = o.optString("cn_name", o.optString("name", id.toString())),
                            types = text?.optString("types")?.takeIf { it.isNotBlank() },
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
    return CardDetail(
        code = code,
        name = details?.optString("cn_name")
            ?: text?.optString("name")
            ?: details?.optString("name"),
        types = text?.optString("types") ?: details?.optString("types"),
        desc = text?.optString("desc") ?: details?.optString("desc"),
        pdesc = text?.optString("pdesc") ?: details?.optString("pdesc"),
        imageUrl = details?.optString("image_url")?.takeIf { it.isNotBlank() }
            ?: text?.optString("image_url")?.takeIf { it.isNotBlank() },
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
