# 服务端 API 对接说明（安卓端补齐用）

> 需求来源基线：`ink9dye/qq-replay-bot@master 34c7feb`；上线功能版本：`076ada2`（2026-11-12）
> 对接方：安卓客户端（`戏王版权局`，`usertools_android`）
> 修订：2026-11-12。2026-11-12 19:33:39已部署并验收；服务端功能提交076ada2，安卓代码未改。
> 本文只含**服务端要提供的东西**：接口契约、复用的现有实现、缓存/并发/错误码、验收命令。客户端 UI 不在本文范围。

## 0. 总览

三组接口，共 7 个端点，全部挂在现有 `/query-api` 前缀下，**不新增监听端口**；复用既有 nginx 路由，实施前核实实际配置已覆盖这些路径。

| # | 端点 | 方法 | 用途 | 复用 |
|---|---|---|---|---|
| 1 | `/query-api/binding` | GET | 查当前身份的 MC 绑定 | `store.get_binding()` |
| 2 | `/query-api/binding` | POST | 绑定 / 改绑 | `store.set_binding()` + 与现有 QQ 一致的名字归一化语义 |
| 3 | `/query-api/binding/unbind` | POST | 解绑 | `store.delete_binding()` |
| 4 | `/query-api/player/{name}/side-plan` | GET | 换备（先攻/后攻分列） | `/statistics/api/side-recent` → `statistics_side_recent.query()` |
| 5 | `/query-api/card/{code}/usage` | GET | 卡牌使用量 | `card_usage_pie.collect()` |
| 6 | `/query-api/card/{code}/flow` | GET | 单卡流向 | `card_flow.collect_flow()` |
| 7 | `/query-api/card/{code}/winrate` | GET | 单卡胜率（含先后攻） | `card_winrate.collect()` |

## 1. 通用约定

**入口**

| 项 | 值 |
|---|---|
| 公网入口 | `https://<服务器>:8443/query-api/`（独立 8443 TLS 入口，现有查询接口同一套） |
| 鉴权 | `Authorization: Bearer <query access token>`（登录 `/query-api/login` 换来的 access，默认 24 h） |
| 身份 | 一律取自 token 的 `identity`（5–20 位 QQ 号），**不接受正文/参数指定别的账号** |

**响应包装**

```json
{"ok": true, "data": {}}
```

失败：`{"ok": false, "error": "<code>"}`

**错误码（沿用现有语义）**

| HTTP | error | 含义 |
|---|---|---|
| 400 | `bad_player` / `bad_code` / `bad_param` | 参数非法（长度、字符、范围） |
| 400 | `player_required` | 必填玩家名缺失 |
| 401 | `unauthorized` | 没带 token / 过期 / 吊销 |
| 404 | `card_not_found` | 本地卡库及既有可信卡号映射均无法识别该卡号 |
| 404 | `no_build` | 该玩家最近 14 天没有可用构筑（仅换备） |
| 429 | `busy` | 新端点排队超时、统计上游繁忙或绑定写入限流；附 `Retry-After` 秒数 |
| 503 | `query_unavailable` | 统计服务不可达、依赖缺失或不能可靠产出结果；不用于正常无记录 |

以上为新增端点契约。现有实现可能返回 `query_busy`（含 503）；新增端点在适配层将可识别的繁忙状态统一为 `429 busy`，不批量修改旧端点语义。保留既有查询关闭等错误；客户端仅对明确可重试的状态退避重试。

正常空结果不等于故障：已知卡牌没有使用记录时返回 `200`，使用相关计数为0，无可展示条目时对应列表为空，无分母的胜率为 `null`。usage.records仍可包含已确认未携带该卡的玩家，flow.items仍可包含使用量为0的卡组；不因无使用记录删除真实覆盖信息。覆盖人数、总人数、未使用人数及“0 张”分布仍由真实统计结果决定，**不能因该卡未使用便将 covered 等覆盖字段全部清零**。上游结果缺失/未完成且无法确认是否确实无记录时返回 503，不伪造空结果。

**缓存与并发**

| 端点 | 缓存 TTL | 并发 |
|---|---|---|
| `binding`（GET） | 不缓存（个人数据） | 不计 heavy |
| 绑定 / 改绑 / 解绑（POST） | 不缓存 | 三种操作合计限流 10 次/分钟/账号 |
| `side-plan` | 30 s（键含 name/digest/top/recent/min_side_games/opponent_range） | 计 heavy（2 路） |
| `card/usage`、`card/flow`、`card/winrate` | 300 s（键含端点、卡牌身份及适用的 limit） | 计 heavy（2 路） |

heavy 闸门复用现有 `/query-api/player/*`、`/query-api/card/*` 的前缀判定（`QueryHandler._HEAVY_PREFIXES`），新路径天然命中。2 路是 API heavy 请求闸门上限，不代表网页、QQ、统计门户之间已有统一的全局计算闸门；部署前须核对实际参数、共享缓存和重计算位置。排队等待沿用现有有界设置，不能仅凭本文件认定固定为 8 s。

统计执行方式固定为：查询层通过既有回环 PortalClient 调用统计门户；缺少卡牌 JSON 内部入口时，在仅回环且沿用 owner 鉴权的统计门户补适配入口，复用既有 collect/缓存。查询进程不另开事实库重算。内部统计入口不得自动暴露到公网白名单。同键并发未命中应合并计算或复用既有串行缓存机制；沿用缓存条数/字节上限，不建立无界缓存。

缓存键须区分端点及全部影响结果的参数；卡号别名共用统计缓存时，`requested_code` 在输出阶段按本次请求填写，避免返回前一请求的卡号。只缓存成功结果，不将临时 503 当作正常空结果缓存。

**日志**：只记启动、失败类型、identity 哈希前缀；不输出 token、明文 QQ 号、玩家名全文、完整路径。

**数据时间**：四个统计端点在 data 中增加 `generated_at`（UTC Unix 秒，可含小数），表示底层结果实际生成时间。底层及外层缓存命中均保留原时间，不能用响应时间冒充；不保证多个独立接口属于同一快照。绑定响应不加此字段。

**响应体积**：不默认传全部 records。用实际响应字节数验收，不承诺未经测量的固定 KB 上限；流向保留三组完整列表的一致性，裁剪规则见 §4.3。

**字段约定**：新增字段向后兼容；客户端忽略未知字段。计数和卡号用 JSON 整数，所有时间戳为 UTC Unix 秒（不是毫秒）。流向 `rate` 为 0–1，单卡胜率 `rate` 为 0–100；无有效分母为 null，真实零胜率为 0。平局进胜率分母，沿用既有算法；未知结果不自行当作负局。

**名称编码**：JSON 使用 UTF-8；玩家名去首尾空白后按 Unicode 码点计长，拒绝控制字符、`/`、`\`。路径参数按单个路径段百分号编码：空格 `%20`、字面加号 `%2B`、百分号 `%25`，服务端只解码一次，不对路径段使用 unquote_plus。不把所有内部空格或加号随意删除。

## 2. 身份绑定

### 2.1 数据模型

直接用现有表，**不新建表、不迁移**：

```sql
CREATE TABLE qq_bindings (
    user_id   TEXT PRIMARY KEY,   -- 查询身份的 QQ 号（与 QQ 端 user_id 同一命名空间）
    player    TEXT NOT NULL,
    updated_at REAL NOT NULL
);
```

因为两边 `user_id` 都是 QQ 号，App 绑定后 QQ 私聊发「历史」（不带名字）会命中同一个玩家；反之亦然。这是本设计的关键，别做成两套。绑定仅是默认查询偏好，不证明拥有该 MC 账号，不授予额外权限。GET 第一版只返回 player，不要求修改 store 以读取 updated_at。

### 2.2 `GET /query-api/binding`

响应：

```json
{"ok": true, "data": {"player": "捏爆你的小旺仔"}}
```

未绑定：`{"ok":true,"data":{"player":null}}` —— **不要返回 404**，客户端据此显示"未绑定"。

### 2.3 `POST /query-api/binding`

请求：

```json
{"player": "捏爆你的小旺仔"}
```

响应：

```json
{"ok": true, "data": {"player": "捏爆你的小旺仔", "known": true, "changed": true}}
```

字段语义：

| 字段 | 说明 |
|---|---|
| `player` | 实际写入的名字（归一化后的真实名） |
| `known` | `store.has_player(player)`：最近 14 天收录里有没有这个名字 |
| `changed` | 与旧绑定不同才为 true |

校验与处理：

1. player 必须为字符串；缺失或去首尾空白后为空 → `400 player_required`；类型错误、超过100个 Unicode 码点、含控制字符或 `/`、`\` → `400 bad_player`。
2. **归一化**：复用 `Service._canonical_player()` 的语义 —— 在 `store.player_names()` 里做"空格归一后唯一匹配"替换（`久 远` → 真实写法）；不唯一或不命中就按原文保存。
3. `known=false` **仍然保存**（可能最近没打），由客户端提示"最近 14 天没收录到这个名字"。
4. 不需要 `query.writes_enabled`：这是个人绑定，不是订阅/统计写入。绑定、改绑、解绑按账号共用上述限流；本版不新增配置开关。
5. 不进入 QQ 那边的 `_bind_pending` 流程，一次调用成事。

### 2.4 `POST /query-api/binding/unbind`

请求无正文。响应：

```json
{"ok": true, "data": {"removed": true}}
```

本来就未绑定 → `removed:false`，**不报错**。

### 2.5 实现位置

- `bot/query_api.py`：GET 分发加 `if path == "/query-api/binding"`；POST 分发加 `/query-api/binding` 与 `/query-api/binding/unbind`。
- `bot/query_core.py`：加 `binding_get(identity)` / `binding_set(identity, player)` / `binding_clear(identity)`，内部调 store（`get_binding` / `set_binding` / `delete_binding` 三个方法已存在，`bot/store.py:403-432`）。
- 归一化：在 query_core 中集中实现查询侧归一化辅助函数，对照现有 Service._canonical_player 语义测试；本版不为抽函数修改 service.py 或重启 QQ。若已有不依赖 Service 的纯函数则直接复用，不导入/实例化 Service。

## 3. 换备（Side Plan）

### 3.1 已有实现，不要重写

`/statistics/api/side-recent`（`bot/statistics_portal.py:629`）已经返回**带中文卡名**的完整 JSON，内部就是 `statistics_side_recent.query()` → `build_plan()`。本接口通过既有回环统计门户做“查询 token 鉴权 + 转发 + 缓存 + 错误码适配”，不重写统计算法。

### 3.2 `GET /query-api/player/{name}/side-plan`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `name`（路径） | 字符串 ≤100 字 | 必填 | 玩家名，百分号编码；按 §1 编码与校验 |
| `digest` | 64 位小写 hex | 空 | 指定某套构筑；空=最新一套 |
| `top` | 1–12 | 6 | 取最常遇到的 N 个对手卡组 |
| `recent` | 1–10 | 5 | 每个对手取最近 N 场 |
| `min_side_games` | 1–100 的整数 | 3 | 低于该值标 `sample_low` |
| `opponent_range` | 200 / 0 | 200 | 只看对手前 200；`0`=关闭过滤。**默认必须 200**，否则数字和 QQ/网页对不上 |

响应：

```json
{"ok":true,"data":{
  "version":1,"generated_at":1793940116.0,
  "player":"捏爆你的小旺仔",
  "deck":"刻魔泪冠",
  "selected":{"digest":"…","recorded_at":1793940116.0,"observation_kind":"first"},
  "total_matches":37,
  "top_decks":6,"recent_matches":5,"min_side_games":3,
  "opponents":[
    {
      "deck":"珠泪哀歌",
      "matches":9,
      "recent_matches":5,
      "side_games":6,
      "seat_unknown_games":0,
      "sample_low":false,
      "first":{
        "side_games":3,
        "added":[{"code":14558127,"name":"灰流丽","partition":"main",
                  "sessions":2,"side_games":2,
                  "copies":{"1":2},"min_copies":1,"max_copies":1}],
        "removed":[{"code":23434538,"name":"增殖的G","partition":"main",
                    "sessions":1,"side_games":1,
                    "copies":{"3":1},"min_copies":3,"max_copies":3}]
      },
      "second":{"side_games":3,"added":[],"removed":[]},
      "sessions":[
        {"id":14930,"recorded_at":1793940116.0,"game_numbers":[1,2,3],
         "side_games":2,"added":[],"removed":[]}
      ]
    }
  ],
  "rank_scope":{"opponent_range":200,"excluded_matches":2,"unverified_matches":1},
  "notes":["只统计当前构筑（主卡相同）的场次；每个对手取最近 5 场。","…"]
}}
```

要点：

- `first` / `second` = **先攻 / 后攻**（按该换边局玩家自己的 `first_player` 座位，不是首局座位）。
- `added` / `removed` 必须补 `name`（直接复用统计门户已做的卡名装饰，不在查询层重复补名；局内 sessions 的卡牌项也遵循同样规则）。
- `copies` 是"张数 → 场次"的直方图；`min_copies`/`max_copies` 是区间，客户端显示成 `1~2 张`。
- `404 no_build`：`query()` 返回 None 时（该玩家最近 14 天没有可用构筑）。QQ 现在回中文句子，API 要换成错误码。
- 已有构筑但没有符合条件的对手场次：200、opponents=[]；与 no_build 区分。指定 digest 对该玩家无可用构筑时也返回 no_build。
- opponent_range=0 在适配层转换为既有实现的关闭过滤值（如 None），不可假定直接传整数 0 语义相同。
- 参数严格校验，非法值返回 400 bad_param，不静默截到边界；整数参数不接受小数、布尔值或重复参数。
- 顺序直接沿用原实现（对手排行、sessions、换入换出卡牌），不在安卓专用接口另排一遍。
- 耗时以实际验收为准；复用现有有界上游超时，不为接口随意放宽到30秒以上。

## 4. 卡牌三接口

三个 collect 已有结构化结果，但还需处理精确卡号适配、空结果、内部统计入口和时间字段，并非仅加 HTTP 外壳。只调用数据收集逻辑，不调用出图函数或新增绘图库依赖。

### 4.1 公共约定

- `{code}`：仅十进制整数，范围 1–4294967295；非法 → 400 bad_code。合法但卡库和既有可信映射均不识别 → 404 card_not_found；仅“没有统计记录”不能判为不存在。
- 卡号通过本地精确查询及既有 card_identity 异画/先行号规则确定身份组；不改变映射、合并算法或参数。不能只把 code 转成卡名再走模糊匹配。现有 collect 接收卡名时，应给数据收集层补精确 code 调用入口，复用原计算核心，不复制算法，也不改变 QQ 原调用行为。
- 返回 requested_code（本次请求整数）、code（既有身份规则实际选用的代表号）、name（该身份中文名）。二者可相同；映射依据必须来自既有可信规则。
- usage 支持 limit=1..100，默认20；winrate 支持 limit=1..20，默认20；flow 不支持 limit，传入返回400 bad_param。limit 必须为整数，非法值返回400 bad_param，不静默截断。
- 每个统计响应 data 含 generated_at。聚合值必须基于完整数据，不能裁剪后重算；不得修改共享缓存字典来裁剪响应。
- 已知卡牌无记录返回正常空统计；服务故障返回503，详见 §1。
- 以下 JSON 只展示字段结构与类型，不是同一批真实数据；空数组/空对象及字符串“…”表示示例省略，不代表实际统计值。实际响应必须为标准 JSON，不含 NaN、Infinity 或省略号字面量。

### 4.2 `GET /query-api/card/{code}/usage`

来源：`card_usage_pie.collect(db, data_dir, now, card_name)`。返回字段（全部保留）：

```json
{"ok":true,"data":{
  "requested_code":14558127,"code":14558127,"name":"灰流丽","generated_at":1793940116.0,
  "covered":86,"current":100,"excluded":0,"total_players":100,
  "main_dist":{"0":2,"1":3,"2":8,"3":87},
  "side_dist":{"0":74,"1":12,"2":9,"3":5},
  "total_dist":{"0":1,"1":2,"2":7,"3":90},
  "main_total":287,"side_total":45,"total_total":332,
  "side_used":26,"side_available":true,
  "records":[
    {"player":"…","rank":1,"main":3,"side":1,"side_out":0,"total":4,
     "main_known":true,"known":true,"build":"…","has_build":true}
  ],
  "limit":20,"records_total":100,"truncated":true
}}
```

records 只取原实现顺序的前 limit 条；records_total 为裁剪前条数，truncated = records_total > len(records)。各分布与总数不受 limit 影响，顺序沿用原实现。

口径提示（写进 `notes` 或字段注释即可，别改算法）：主卡只认**确证第 1 局**；`side` 是从第 2/3 局相对首局的净换入反推，录像本身不含备牌表。

### 4.3 `GET /query-api/card/{code}/flow`

来源：`card_flow.collect_flow(...)`。返回：

```json
{"ok":true,"data":{
  "requested_code":14558127,"code":14558127,"name":"灰流丽","generated_at":1793940116.0,
  "covered":86,"current":100,"total_players":100,
  "main_used":87,"side_used":26,"total_used":90,
  "items":[
    {"deck":"珠泪哀歌","players":12,
     "main":{"known":12,"used":12,"one":1,"two":2,"three":9,"rate":1.0},
     "side":{"known":12,"used":4,"one":1,"two":1,"three":2,"rate":0.333},
     "total":{"known":12,"used":12,"one":1,"two":1,"three":10,"rate":1.0}}
  ],
  "bars":{"main":[],"side":[],"total":[]},
  "small_groups":[{"deck":"…","players":2,"main":{},"side":{},"total":{}}]
}}
```

`items` 为全部卡组、`bars` 仅包含达到现有样本阈值的卡组、`small_groups` 为低样本卡组。第一版三者完整保留，不支持 limit、不独立截断；顺序和阈值均沿用 collect_flow。用真实全量响应测量体积，不承诺 <32 KB。

### 4.4 `GET /query-api/card/{code}/winrate`

来源：`card_winrate.collect(...)`。返回：

```json
{"ok":true,"data":{
  "requested_code":14558127,"code":14558127,"name":"灰流丽","generated_at":1793940116.0,
  "matchups":{
    "version":1,"unit":"used_small_game_perspective",
    "population":"top100","partition":"main",
    "limit":20,"min_games":10,"fallback_games":5,
    "base":{"games":1240,"wins":660,"losses":540,"draws":40,
            "rate":53.23,"unknown_outcome":0},
    "rows":[{"deck_type":"珠泪哀歌","games":120,"wins":70,"losses":50,
             "rate":58.33,"low_sample":false}],
    "total_decks":18,"qualified_decks":11,
    "notes":["…"]
  },
  "order":{
    "first":{"games":640,"wins":350,"losses":290,"draws":0,"rate":54.69,"unknown_outcome":0},
    "second":{"games":600,"wins":310,"losses":250,"draws":40,"rate":51.67,"unknown_outcome":0},
    "unknown":{"games":0,"wins":0,"losses":0,"draws":0,"rate":null,"unknown_outcome":0}
  }
}}
```

`order.first` / `order.second` 是**先攻/后攻**（现有分组），直接来自 `card_winrate._compute` 里的 `order`，不要自己重算。

`matchups.rows` 对安卓 API 默认最多20项，支持 limit=1..20；样本不足时可更少。matchups.limit 返回本次有效选择上限。保留 total_decks、qualified_decks 原有含义，不将其直接解释成可分页总数。未知座位只进入总体及 order.unknown，不挪入先攻或后攻。

实现要求：

- 现有 MATCHUP_LIMIT=5 在计算阶段截断；必须将条数作为可选参数传入 collect → _compute → matchups，再进入筛选计算。不能只在 HTTP 层对既有5条结果切片，也不能把全局 MATCHUP_LIMIT 直接改成20。
- 数据收集层新增可选参数，默认值保持5以兼容原 QQ 调用；安卓 API 未传 limit 时显式传20。网页及其他调用方沿用各自原默认行为。
- API及底层计算缓存键均须包含有效 limit，避免 QQ Top5 和安卓 Top20 共用被截断结果。总体及 order 的统计不受 limit 影响，继续基于完整样本。
- 沿用现有最低样本数、补位样本数和排序规则；将现有“合格项不足目标条数时才补低样本”的目标从固定5改为本次 limit。默认 API 即不足20项时按既有低样本规则补位，并保留 low_sample 标记；不降低样本阈值来凑满20项。
- 不承诺 Top20 的前5项一定等于另算 Top5：补位项参与既有排序时，条数可能影响最终展示。API 显式 limit=5 时必须与 QQ Top5 的筛选和顺序一致。

## 5. 改动位置清单（给服务器开发）

| 文件 | 改什么 |
|---|---|
| `bot/query_api.py` | GET 分发：`/query-api/binding`、`/query-api/player/{name}/side-plan`、`/query-api/card/{code}/(usage\|flow\|winrate)`；POST 分发：`/query-api/binding`、`/query-api/binding/unbind` |
| `bot/query_core.py` | 新增 `binding_get/set/clear`、`side_plan(name, ...)`、`card_usage(code, limit)`、`card_flow(code)`、`card_winrate(code, limit)`；统计统一经回环 portal，含错误与输出字段适配 |
| `bot/store.py` | 无需改（`get_binding`/`set_binding`/`delete_binding` 已在） |
| `bot/service.py` | 本版不修改；仅参考名字归一化行为 |
| 卡牌数据收集模块 | 必要时补精确卡号入口及生成时间，复用原计算与缓存；原 QQ 入口和统计规则保持兼容 |
| `bot/statistics_portal.py` | 复用 side-recent，补仅回环 owner 鉴权的卡牌 JSON 适配；避免在 query 进程重复计算 |
| `docs/QUERY_API.md` | 新增 §10，补三组端点契约 |

配置：**不需要新增配置项**。本版不新增 binding_enabled；所有端点跟随 query.enabled。既有订阅写权限控制保持不变。

## 6. 验收命令

```bash
# Bash 示例；测试账号应是专用账号。不要将真实令牌写入文档、日志或命令历史。
read -r -s -p 'Query access token: ' TOKEN; echo
B='https://<服务器>:8443'
# 现有入口若为自签证书，请配置受信证书或 curl --cacert；不要用 -k 跳过校验。

# 1 绑定
curl -s -H "Authorization: Bearer $TOKEN" $B/query-api/binding
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"player":"久 远"}' $B/query-api/binding
curl -s -X POST -H "Authorization: Bearer $TOKEN" $B/query-api/binding/unbind

# 2 换备（对方须与 QQ「换备 玩家名」数字一致）
curl -s -H "Authorization: Bearer $TOKEN" \
  "$B/query-api/player/$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1], safe=''))' '捏爆你的小旺仔')/side-plan?top=6&recent=5"

# 3 卡牌三接口
curl -s -H "Authorization: Bearer $TOKEN" $B/query-api/card/14558127/usage
curl -s -H "Authorization: Bearer $TOKEN" $B/query-api/card/14558127/flow
curl -s -H "Authorization: Bearer $TOKEN" $B/query-api/card/14558127/winrate
```

验收要点：

1. 未带 token → 全部 401；带**观战令牌**（`wt_`）或**管理令牌** → 401（用途分离，不要互相认）。
2. 绑定往返：App 绑定后 QQ 私聊「历史」不带名字能查到同一玩家；解绑后 QQ 提示未绑定。
3. 换备数字与 QQ「换备」文字逐项一致（对手顺序、场次、先/后攻的换入换出）。
4. 卡牌统计在同一数据快照下与 QQ 一致；单卡胜率比较总体及先后攻时忽略展示条数差异，比较对阵列表时 API 显式使用 limit=5。安卓默认Top20另按条数规则验收。
5. 已知冷门卡无记录 → 200；分布、覆盖人数如实保留，使用计数为0，无分母胜率null。不存在卡号 →404；上游故障 →503；不得互相替代。
6. 有界并发测试：使用可控慢查询确认 API heavy 同时在途最多2路；未获槽位且等待超时返回429并附 Retry-After。第3个请求可能排队后成功或命中缓存，不要求必定失败。另验同键冷缓存合并、不同键负载不拖住观战、缓存容量有上限。不得在生产无控制压测。
7. 编码：测试名字中的普通空格、双空格、字面 +、%、中文；只解码一次；编码斜杠、反斜杠、控制字符拒绝。
8. 身份组：对已有可信映射的正式号、异画号、先行号查询，聚合结果一致；requested_code 各自准确，缓存不会串号；同名但未被映射为同一卡的卡牌不擅自合并。
9. 条数：usage limit=1/20/100 的聚合值完全一致，records_total/truncated 正确；flow 传limit返回400。winrate 未传limit时 matchups.limit=20、rows最多20项；传1/5/20有效，0/21/非整数返回400。limit=5与 QQ 原Top5结果一致，原QQ调用默认仍为5；不同limit缓存不串用，总体及order不受条数影响。覆盖合格样本不足、低样本补位和不足20项的场景。
10. 缓存时间：命中底层或外层缓存 generated_at 不刷新；成功重新生成才更新。接口对比采用固定数据或同一底层缓存快照，不要求不同时刻的线上数据逐字相同。
11. 绑定：重复绑定 changed=false，重复解绑 removed=false；未知名仍保存；两种POST共用账号限流。客户端正文不能选别的 identity，跨账号隔离；重复失败不得修改旧绑定。
12. 验收保留请求路径模板、状态、字段断言及脱敏结果；不保存令牌、QQ号或完整玩家名。真实 QQ 指令验收只在维护者授权下进行，可先用同库读取或已有固定样本比对，不自动发消息。

## 7. 边界

- 全部只读（绑定除外，写 `qq_bindings` 单表），不写事实库、不改统计口径、不碰录像目录。
- 不新增监听端口；核实既有 nginx 查询白名单覆盖新路径并继续隔离 admin。不得将新增内部统计入口公开。
- 卡牌三接口会占 heavy 闸门（2 路）：客户端串行调用，服务端靠300 s缓存吸收重复。胜率Top20须验证冷查耗时与响应体积，复用同一批完整样本计算，不为20行重复扫描20次。
- side-plan 复用现有构筑/统计链路，沿用有界超时；不承诺未经测量的1–3秒耗时。
- 安卓代码、APK、NapCat、QQ链路不在本次修改范围；附件中的开发建议不构成部署或重启授权。
- 服务端只在 master 开发；实施前核实实际线上文件与当前工作区，遵守冻结清单。线上重启须维护者另行同意并避开正在录制对局；尤其不得为复用归一化而顺带重启 QQ。
- 部署需本地编译/相关测试、线上最新文件差异核对及备份、逐文件部署/chown、经授权重启对应服务、健康与新接口实测、记录 SHA256 与回滚办法，更新项目交接。文档本身不是已上线证明。

## 8. 交付与上线状态（2026-11-12）

- 19:33:39已部署6个服务端文件，重启web/collector；用户明确允许本次带远程在录重启。QQ/NapCat及安卓代码未动。
- 7个端点均已接入；胜率API默认20条、支持1–20，QQ原入口默认5条。Python3.12隔离48项通过；公网TLS、测试身份绑定共享读取、真实卡牌统计/换备、Top20与Top5总体和先后攻一致性已验收。
- 内部入口为 /statistics/api/query-side-plan 与 /statistics/api/query-card/{usage|flow|winrate}，要求本机来源、Host、owner cookie及X-Query-Internal: 1；公开统计代理不转发内部头。这些路径不属于公网契约。
- 冷查usage/winrate曾出现约8秒503，有限退避重试后200；客户端必须处理暂时不可用，不以此判定该卡无记录，不无限重试或同时重复请求。未扩大服务端超时。
- 临时测试账号、令牌及绑定已清理，未保存凭据，未发真实QQ消息；安卓UI及真实QQ命令逐项比对尚未执行。
- 功能提交076ada2，已有生产观测依赖归档7536876；详细部署版本与回滚见仓库docs/WRAPUP_QUERY_API_ANDROID_20261112.md。回滚只恢复6个服务文件的本轮生产备份，不回滚数据库；重启需遵守项目规则。
