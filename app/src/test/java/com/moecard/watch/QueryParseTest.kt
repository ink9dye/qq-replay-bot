package com.moecard.watch

import com.moecard.watch.query.model.PieCategory
import com.moecard.watch.query.model.BuildCard
import com.moecard.watch.ui.charts.pieCategoriesOrdered
import com.moecard.watch.query.model.BuildData
import com.moecard.watch.query.model.DeckThumbs
import com.moecard.watch.query.model.dpCurve
import com.moecard.watch.query.model.parseArenaHistory
import com.moecard.watch.query.model.parseArenaProfile
import com.moecard.watch.query.model.parseBuild
import com.moecard.watch.query.model.parseCard
import com.moecard.watch.query.model.parsePie
import com.moecard.watch.query.model.parseReplays
import com.moecard.watch.query.model.parseRoster
import com.moecard.watch.query.model.parseSearch
import com.moecard.watch.query.model.parseSubscriptions
import com.moecard.watch.query.model.parseReplaySearch
import com.moecard.watch.query.model.parseTokenPair
import com.moecard.watch.query.model.resultFor
import com.moecard.watch.ui.query.buildYdk
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 双数据源解析单测：fixture 全部按服务端源码确认的字段构造
 * （query_api.py / query_auth.py / arena_profile.py / player_history.py /
 *  statistics_dashboard.py / card_statistics.py / 线上 sapi 实测响应）。
 */
class QueryParseTest {

    // ---------- 会话 ----------

    @Test
    fun loginResponseParsesTokenPair() {
        val json = JSONObject(
            """
            {"ok":true,"token":"acc-1","refresh_token":"ref-1",
             "access_expires_at":1770000000,"refresh_expires_at":1900000000,
             "identity":"12345678","role":"user","writes":true}
            """.trimIndent()
        )
        val pair = parseTokenPair(json)!!
        assertEquals("acc-1", pair.token)
        assertEquals("ref-1", pair.refreshToken)
        assertEquals(1770000000L, pair.accessExpiresAt)
        assertEquals(1900000000L, pair.refreshExpiresAt)
        assertEquals("12345678", pair.identity)
        assertEquals("user", pair.role)
        assertTrue(pair.writes)
    }

    @Test
    fun loginResponseWithoutTokensReturnsNull() {
        assertNull(parseTokenPair(JSONObject("""{"ok":true,"identity":"1"}""")))
    }

    // ---------- 官方 arena ----------

    /** 线上实测（sapi.moecube.com，test123）就是这种 dict 形状。 */
    @Test
    fun arenaProfileDictShape() {
        val json = JSONObject(
            """
            {"exp":100,"pt":1234.5,"entertain_win":1,"entertain_lose":2,"entertain_draw":0,
             "entertain_all":3,"entertain_wl_ratio":"0","exp_rank":88,
             "athletic_win":30,"athletic_lose":10,"athletic_draw":2,"athletic_all":42,
             "athletic_wl_ratio":"3","arena_rank":15}
            """.trimIndent()
        )
        val p = parseArenaProfile(json)!!
        assertEquals(1234.5, p.pt, 1e-9)
        assertEquals(15, p.arenaRank)
        assertEquals(42, p.athleticAll)
        assertEquals(30, p.athleticWin)
        assertEquals(10, p.athleticLose)
        assertEquals(2, p.athleticDraw)
        assertEquals(71, p.winRate) // 30/42 = 71.4% → 71
    }

    /** 服务端 arena_profile.py 也兼容 list 形状（取第一个）。 */
    @Test
    fun arenaProfileListShape() {
        val arr = JSONArray("""[{"pt":1000,"arena_rank":0,"athletic_all":0,"athletic_win":0}]""")
        val p = parseArenaProfile(arr)!!
        assertEquals(1000.0, p.pt, 1e-9)
    }

    @Test
    fun arenaProfileWithoutPtReturnsNull() {
        assertNull(parseArenaProfile(JSONObject("""{"arena_rank":1}""")))
    }

    /** 官方 history：双方识别、胜负判定、A/B 侧赛前赛后分、非 athletic 行剔除、去重。 */
    @Test
    fun arenaHistoryParsesSidesAndResult() {
        val json = JSONObject(
            """
            {"total":3,"data":[
              {"type":"athletic","usernamea":"alice","usernameb":"bob",
               "start_time":1000,"end_time":1100,"winner":"alice",
               "pta_ex":1200,"pta":1215,"ptb_ex":1100,"ptb":1085},
              {"type":"athletic","usernamea":"carol","usernameb":"alice",
               "start_time":900,"end_time":950,"winner":"none",
               "pta_ex":1195,"pta":1195,"ptb_ex":1190,"ptb":1190},
              {"type":"entertain","usernamea":"alice","usernameb":"dave",
               "start_time":800,"end_time":850,"winner":"alice",
               "pta_ex":1200,"pta":1200,"ptb_ex":1000,"ptb":1000}
            ]}
            """.trimIndent()
        )
        val matches = parseArenaHistory(json, "alice")
        assertEquals(2, matches.size)
        // 升序：900 的在前
        val draw = matches[0]
        assertEquals("carol", draw.opponent)
        assertNull(draw.won) // 平局
        assertEquals(1190.0, draw.ptBefore, 1e-9)
        assertEquals(1190.0, draw.ptAfter, 1e-9)
        val win = matches[1]
        assertEquals("bob", win.opponent)
        assertEquals(true, win.won)
        assertEquals(1200.0, win.ptBefore, 1e-9)
        assertEquals(1215.0, win.ptAfter, 1e-9)
    }

    @Test
    fun arenaHistoryDedupesSameStartEnd() {
        val row = """{"type":"athletic","usernamea":"a","usernameb":"me",
            "start_time":1000,"end_time":1100,"winner":"a",
            "pta_ex":100,"pta":110,"ptb_ex":200,"ptb":190}"""
        val merged = JSONObject().put(
            "data",
            JSONArray("[$row,$row]")
        )
        assertEquals(1, parseArenaHistory(merged, "me").size)
    }

    /** 现网 history 的时间是 ISO-8601 字符串，不能只当数字解析（否则只剩一条）。 */
    @Test
    fun arenaHistoryParsesIsoTimes() {
        val json = JSONObject(
            """
            {"total":2,"data":[
              {"type":"athletic","usernamea":"me","usernameb":"x",
               "start_time":"2026-11-06T09:08:40.000Z","end_time":"2026-11-06T09:11:30.000Z",
               "winner":"me","pta_ex":100,"pta":110,"ptb_ex":0,"ptb":0},
              {"type":"athletic","usernamea":"me","usernameb":"y",
               "start_time":"2026-11-05T07:56:14.000Z","end_time":"2026-11-05T08:08:09.000Z",
               "winner":"y","pta_ex":110,"pta":95,"ptb_ex":0,"ptb":0}
            ]}
            """.trimIndent()
        )
        val matches = parseArenaHistory(json, "me")
        assertEquals(2, matches.size)
        assertEquals(1_793_956_120L, matches[1].startTime) // 2026-11-06T09:08:40Z
        assertEquals(1_793_865_374L, matches[0].startTime) // 2026-11-05T07:56:14Z
    }

    /** DP 曲线 = 首局赛前分 + 每局赛后分（对齐 player_history.py）。 */
    @Test
    fun dpCurveStartsWithPreMatchPoint() {
        val json = JSONObject(
            """
            {"total":2,"data":[
              {"type":"athletic","usernamea":"me","usernameb":"x",
               "start_time":1,"end_time":2,"winner":"me","pta_ex":100,"pta":110,"ptb_ex":0,"ptb":0},
              {"type":"athletic","usernamea":"me","usernameb":"y",
               "start_time":3,"end_time":4,"winner":"y","pta_ex":110,"pta":95,"ptb_ex":0,"ptb":0}
            ]}
            """.trimIndent()
        )
        val curve = dpCurve(parseArenaHistory(json, "me"))
        assertEquals(listOf(100.0, 110.0, 95.0), curve)
    }

    // ---------- 自建查询 API ----------

    /** top100：门户 roster 快照形状（people + deck_distribution + 覆盖数）。 */
    @Test
    fun rosterSnapshotShape() {
        val json = JSONObject(
            """
            {"generated_at":1,"current_players":100,"covered_players":80,
             "unidentified_players":20,"missing_players":0,
             "people":[
               {"player":"alice","rank":1,"points":2000.5,"observed":true,
                "deck_type":"蛇眼","classification_status":"ok","series":"SnS"},
               {"player":"bob","rank":2,"points":1900,"observed":false}
             ],
             "deck_distribution":[{"name":"蛇眼","players":12},{"name":"尤贝尔","players":8}]}
            """.trimIndent()
        )
        val r = parseRoster(json)
        assertEquals(2, r.people.size)
        assertEquals("alice", r.people[0].player)
        assertEquals(1, r.people[0].rank)
        assertEquals(2000.5, r.people[0].points, 1e-9)
        assertEquals("蛇眼", r.people[0].deckType)
        assertEquals(listOf("SnS"), r.people[0].series) // 老版本是字符串
        assertNull(r.people[1].deckType)
        assertEquals(80, r.covered)
        assertEquals(100, r.current)
        assertEquals(20, r.unidentified)
        assertEquals(2, r.deckDistribution.size)
        assertEquals(12, r.deckDistribution[0].players)
    }

    /** top100 若直接给列表也能解析（防御两种形状）。 */
    @Test
    fun rosterListShape() {
        val arr = JSONArray(
            """[{"player":"a","rank":1,"points":10,"observed":true},
                {"player":"b","rank":2,"points":9,"observed":false}]"""
        )
        val r = parseRoster(arr)
        assertEquals(2, r.people.size)
        assertEquals(0, r.covered)
    }

    /** 现行服务端 series 是数组，用于卡组缩略图的系列映射。 */
    @Test
    fun rosterSeriesArrayShape() {
        val json = JSONObject(
            """{"people":[{"player":"alice","rank":1,"points":10,"observed":true,
                "deck_type":"混沌三幻魔水晶机巧","series":["三幻魔","水晶机巧","混沌"]}]}"""
        )
        val r = parseRoster(json)
        assertEquals(listOf("三幻魔", "水晶机巧", "混沌"), r.people[0].series)
    }

    @Test
    fun buildShape() {
        val json = JSONObject(
            """
            {"person":{"player":"alice","rank":1,"points":2000,"observed":true,"deck_type":"蛇眼"},
             "cards":[
               {"partition":"main","code":94820406,"copies":3,"name":"原罪宝-蛇眼"},
               {"partition":"main","code":26439287,"copies":1},
               {"partition":"extra","code":1861629,"copies":1},
               {"partition":"side","code":54631665,"copies":2}
             ],
             "completeness":{"main":true,"extra":true,"side":false},
             "source":{"digest":"abc","recorded_at":1,"expires_at":2},
             "roster_version":1}
            """.trimIndent()
        )
        val b = parseBuild(json)!!
        assertEquals(4, b.cards.size)
        assertEquals("alice", b.person?.player)
        assertTrue(b.mainComplete)
        assertTrue(b.extraComplete)
        assertEquals(false, b.sideComplete)
        assertEquals(3, b.cards[0].copies)
        assertEquals("main", b.cards[0].partition)
        assertEquals("原罪宝-蛇眼", b.cards[0].name)
        assertNull(b.cards[1].name)
    }

    /** 构筑 → .ydk：段落顺序照 YGOMobile，卡号按 copies 重复。 */
    @Test
    fun ydkExportMatchesYgomobileFormat() {
        val build = BuildData(
            person = null,
            cards = listOf(
                BuildCard("main", 10045475, 3),
                BuildCard("extra", 4063756, 1),
                BuildCard("side", 84192580, 2),
            ),
            mainComplete = true,
            extraComplete = true,
            sideComplete = true,
        )
        assertEquals(
            "#created by MoeCardQuery\n#main\n10045475\n10045475\n10045475\n" +
                "#extra\n4063756\n!side\n84192580\n84192580\n",
            buildYdk(build),
        )
    }

    /** 线上 search?type=card 的真实形状：{cards:[{code,name}]}，不是 ygocdb 的 result/id。 */
    @Test
    fun cardSearchUsesServerCardsShape() {
        val json = JSONObject("""{"cards":[{"code":89631139,"name":"青眼白龙"}],"limit":20}""")
        val r = parseSearch("card", json)
        assertEquals(1, r.cards.size)
        assertEquals(89631139L, r.cards[0].id)
        assertEquals("青眼白龙", r.cards[0].name)
    }

    /** 服务端 pie 三层数据（bot/deck_pie.py 输出）：内环大类 / 中环交集 / 外环卡组，带代表卡 image。 */
    @Test
    fun pieLayersParseServerShape() {
        val json = JSONObject(
            """
            {"items":[{"name":"蛇眼","count":5}],"covered":5,"current":7,"unidentified":2,
             "pie":{"total":5,"threshold":3,
               "inner":[{"name":"蛇眼","players":5,"percent":100.0,"color":"#C0392B","image":10045474,"band_image":10045474}],
               "middle":[{"primary":"蛇眼","label":"独占","extras":[],"players":5,"percent":100.0,
                          "color":"#C0392B","accent":null,"image":10045474,"mixed":null}],
               "outer":[{"name":"味美喵蛇眼混沌","players":5,"percent":100.0,"primary":"蛇眼","extras":[],
                         "shared":false,"color":"#D64545","accent":null,"image":10045474}],
               "inner_percent":100.0,"middle_percent":100.0,"outer_percent":100.0,
               "qualified":["蛇眼"],"note":"外环为具体卡组名。"}}
            """.trimIndent()
        )
        val d = parsePie(json)
        val pie = d.pie
        assertNotNull(pie)
        assertEquals(5, pie!!.total)
        assertEquals(1, pie.inner.size)
        assertEquals(10045474L, pie.inner[0].image)
        assertEquals("#C0392B", pie.middle[0].color)
        assertEquals(1, pie.outer.size)
        assertEquals(10045474L, pie.outer[0].image)
        assertTrue(pie.note.startsWith("外环"))
    }

    /** 饼图大类顺序：人数降序，「其他」倒数第二，「未识别」最后。 */
    @Test
    fun pieCategoriesPutOtherSecondLastAndUnknownLast() {
        val cats = listOf(
            PieCategory("其他", 32, 32.0, "#999", null, null),
            PieCategory("无垢大艺术", 21, 21.0, "#111", 1L, null),
            PieCategory("未识别", 1, 1.0, "#777", null, null),
            PieCategory("混沌", 18, 18.0, "#222", 2L, null),
        )
        assertEquals(
            listOf("无垢大艺术", "混沌", "其他", "未识别"),
            pieCategoriesOrdered(cats).map { it.name },
        )
    }

    /** 移植 bot/deck_pie.py::match_fields：长字段优先、被包含的短字段丢弃、结果按出现位置排序。 */
    @Test
    fun matchFieldsPrefersLongestAndDropsContained() {
        val fields = DeckThumbs.matchFields("混沌三幻魔", listOf("三幻魔", "幻魔", "混沌"))
        assertEquals(listOf("混沌", "三幻魔"), fields)
        assertEquals(listOf("味美喵", "蛇眼"), DeckThumbs.matchFields("味美喵蛇眼混沌", listOf("蛇眼", "味美喵")))
        assertEquals(emptyList<String>(), DeckThumbs.matchFields("烙印星辰", listOf("蛇眼")))
    }

    /** 录像库 v2：一场 = 1~2 局，逐局带双方视角，可按场算胜负。 */
    @Test
    fun replaySearchParsesMatchAndGames() {
        val json = JSONObject(
            """
            {"ok":true,"page":1,"page_size":30,"total":1,"items":[{
              "key":"20261106_ss","session":"ss","start":1793940116.0,"end":1793940823.0,
              "players":["Me","Opp"],"decks":{"Me":"烙印星辰","Opp":"码丽丝"},
              "score":"2:1","winner":"Me","decided":3,"game_count":3,"bytes":900,
              "folder":"replays/2026/11/06/ss","available":true,
              "download":"/replay-api/download?match=ss",
              "games":[
                {"id":14930,"game_no":1,"name":"2026-11-06 11-21-56.yrp","size":300,
                 "download":"/replay-api/download?id=14930",
                 "views":[{"player":"Me","outcome":"win","deck":"烙印星辰"}]},
                {"id":14931,"game_no":2,"name":"2026-11-06 11-30-11.yrp","size":300,
                 "download":"/replay-api/download?id=14931",
                 "views":[{"player":"Me","outcome":"loss","deck":"烙印星辰"}]}
              ]}]}
            """.trimIndent()
        )
        val res = parseReplaySearch(json)
        assertEquals(1, res.total)
        val m = res.items.single()
        assertEquals("Opp", m.players[1])
        assertEquals("烙印星辰", m.decks["Me"])
        assertEquals(2, m.games.size)
        assertEquals(1_793_940_116L, m.start)
        assertEquals("win", m.resultFor("Me"))
        assertEquals("loss", m.resultFor("Opp"))
        assertEquals("/replay-api/download?id=14930", m.games[0].download)
    }

    @Test
    fun replaysShape() {
        val json = JSONObject(
            """
            {"items":[
               {"id":"r1","name":"alice-vs-bob.yrp","recorded_at":1770000000,
                "url":"https://example.test/file/r1"}
             ],
             "link":{"url":"https://example.test/dir/abc","expires_at":1770086400,"public":true}}
            """.trimIndent()
        )
        val r = parseReplays(json)
        assertEquals(1, r.items.size)
        assertEquals("alice-vs-bob.yrp", r.items[0].name)
        assertEquals("https://example.test/file/r1", r.items[0].url)
        assertNotNull(r.link)
        assertEquals(1770086400L, r.link!!.expiresAt)
        assertEquals(true, r.link!!.isPublic)
    }

    @Test
    fun replaysWithoutLink() {
        val r = parseReplays(JSONObject("""{"items":[]}"""))
        assertEquals(0, r.items.size)
        assertNull(r.link)
    }

    @Test
    fun pieShape() {
        val json = JSONObject(
            """
            {"items":[{"name":"蛇眼","count":30},{"name":"尤贝尔","count":20}],
             "pie":"x","covered":80,"current":100,"unidentified":20}
            """.trimIndent()
        )
        val p = parsePie(json)
        assertEquals(2, p.items.size)
        assertEquals(30, p.items[0].count)
        assertEquals(80, p.covered)
        assertEquals(100, p.current)
    }

    @Test
    fun searchDeckShape() {
        val json = JSONObject(
            """{"items":[{"name":"蛇眼","players":12}],"limited":true}"""
        )
        val r = parseSearch("deck", json)
        assertEquals(1, r.decks.size)
        assertEquals("蛇眼", r.decks[0].name)
        assertTrue(r.limited)
    }

    /** 卡片搜索：ygocdb 透传形状。 */
    @Test
    fun searchCardShape() {
        val json = JSONObject(
            """
            {"result":[
               {"id":94820406,"cid":1,"cn_name":"原罪宝-蛇眼",
                "text":{"name":"原罪宝-蛇眼","types":"魔法/通常","desc":"…","pdesc":""}}
             ],"next":null}
            """.trimIndent()
        )
        val r = parseSearch("card", json)
        assertEquals(1, r.cards.size)
        assertEquals(94820406L, r.cards[0].id)
        assertEquals("原罪宝-蛇眼", r.cards[0].name)
        assertEquals("魔法/通常", r.cards[0].types)
    }

    /** 玩家搜索：门户 people 列表形状。 */
    @Test
    fun searchPlayerShape() {
        val json = JSONObject(
            """{"people":[{"player":"alice","rank":1,"points":10,"observed":true}],"limited":false}"""
        )
        val r = parseSearch("player", json)
        assertEquals(1, r.players.size)
        assertEquals("alice", r.players[0].player)
    }

    /** 卡片详情：details（查卡透传）+ metrics（可为 null）。 */
    @Test
    fun cardDetailShape() {
        val details = JSONObject(
            """{"id":94820406,"cn_name":"原罪宝-蛇眼",
                "text":{"name":"原罪宝-蛇眼","types":"魔法/通常","desc":"效果文本","pdesc":""}}"""
        )
        val c = parseCard(94820406, details, null)
        assertEquals("原罪宝-蛇眼", c.name)
        assertEquals("魔法/通常", c.types)
        assertEquals("效果文本", c.desc)
        assertNull(c.metrics)
        assertNull(c.imageUrl)
    }

    /** 现行服务端 /query-api/card/<code> 用 details.{name,description}，没有 text 包裹。 */
    @Test
    fun cardDescriptionShape() {
        val details = JSONObject(
            """{"code":59400890,"name":"毁灭之黑魔术师","description":"效果文本一\n效果文本二",
                "base_partition":"extra","allowed_partitions":["extra","side"]}"""
        )
        val c = parseCard(59400890, details, null)
        assertEquals("毁灭之黑魔术师", c.name)
        assertEquals("效果文本一\n效果文本二", c.desc)
        assertNull(c.types)
        assertNull(c.pdesc)
    }

    @Test
    fun subscriptionsShape() {
        val json = JSONObject(
            """
            {"items":[
               {"player":"alice","rank_top":50,"notify":true},
               {"player":"bob","rank_top":null,"notify":false}
             ]}
            """.trimIndent()
        )
        val subs = parseSubscriptions(json)
        assertEquals(2, subs.size)
        assertEquals(50, subs[0].rankTop)
        assertTrue(subs[0].notify)
        assertNull(subs[1].rankTop)
    }
}
