# 受控压测结果：A+B 之后的安卓端查询上限（2026-11-07 22:26–22:33）

- 目标：`https://127.0.0.1:8443`（服务器本机，nginx → collector → portal），请求序列按安卓 1.9.1：top100 → pie → player decks → player replays → card，思考时间 2 s。
- 账号：管理员签发的专用测试账号 `90000000001`，测完已 disable；token 只在服务器内存/临时文件里，未输出、未落盘。
- 阶段：1 / 2 / 4 并发，每阶段 90 s；服务端每 2 s 采样。
- 环境：A（MemoryHigh 480M/560M + zram）与 B（roster 预计算）已生效；测试期间无 abort。
- 证据：`workflow-results/stress-20261107-2230/stress.json`（逐请求 status/字节/耗时）。

## 1. 结果

| 阶段 | 请求 | 200 | 503 | 出口流量 | 折算 |
|---:|---:|---:|---:|---:|---:|
| 1 用户 | 33 | 33 | 0 | 0.18 MB / 90 s | 0.02 Mbps |
| 2 用户 | 64 | 64 | 0 | 0.34 MB / 90 s | 0.03 Mbps |
| 4 用户 | 156 | 153 | 3 | **10.53 MB / 90 s** | **0.94 Mbps** |

分接口（4 用户阶段）：

| 接口 | n | ok | p50 | p95 | max | 平均 wire |
|---|---:|---:|---:|---:|---:|---:|
| top100 | 4 | 4 | 166 ms | 187 ms | 187 ms | 15.0 KB |
| charts/pie | 39 | 39 | 24 ms | 237 ms | 661 ms | 3.1 KB |
| player/decks | 39 | 39 | 60 ms | 206 ms | 2.9 s | 2.6 KB |
| player/replays | 39 | 36 | 47 ms | 271 ms | 352 ms | 13.2 KB |
| card | 35 | 35 | 340 ms | 8.0 s | 8.1 s | **278 KB**（中位 311 B，最大 608 KB） |

服务端采样（141 个点，全程）：`available` 387–648 MB、web 43–429 MB、swap ≤57 MB、load1 ≤3、PSI full ≤8。**没有内存/CPU 压力，没有触发任何中止线。**

## 2. 关键发现

1. **roster 修好后，top100 不再是大头**：15 KB、19–166 ms（预计算文件）；pie 3 KB、24 ms。
2. **卡牌响应是唯一的带宽大头**：
   - 一次“有统计”的卡牌响应 = 原始 **2,112,492 B**，gzip 后 **607,961 B**；
   - 其中 `metrics.samples` 占 **1,774,266 B（84%）**：6,645 条逐局记录，每条含 digest(64 hex)、player、build hash、deck_type、outcome 等；
   - 其余 `by_deck` 119 KB、`investment` 76 KB；
   - 4 用户阶段 35 次卡牌请求里 10 次是 608 KB、其余是 details-only（311–507 B），合计 **9.74 MB**，占该阶段出口的 **92.5%**。
3. **卡牌有“返回 details-only”的降级**：58 次卡牌请求里只有 10 次带回完整 metrics；其余等 8 秒后只返回 311–507 B。客户端很可能看到“统计不可用/空页”。
4. **卡牌首次计算 8 秒，缓存后 0.3 秒**：p50 340 ms、p95 8.0 s；命中 `response_get` 后很快，冷计算仍贵。
5. **4 用户开始出现 replays 503**：3/39（7.7%），是 deck-detail 单飞等待，不是 roster。

## 3. 上限推算

- 内存/CPU：4 用户只用了 0.94 Mbps、web ≤429 MB、load ≤3；按资源看 8–10 人没问题（未测，需下一轮）。
- **带宽：4 用户 0.94 Mbps，其中 0.87 Mbps 是卡牌**；线性外推，**约 12 个“正在翻卡牌”的用户会把 3 Mbps 打满**。
- 若卡牌 payload 不裁，同时 10+ 人看卡就一定卡；roster/pie/decks/replays 加起来只有 ~0.1 Mbps。

## 4. 建议（按收益）

1. **裁 `metrics.samples`**：卡牌页不需要 6,645 条逐局记录；保留最近 50–100 条或只给聚合（by_deck/investment 已够）。预期 608 KB → 40–60 KB，带宽上限 ×10，卡牌页也更快。**这会改变接口返回内容，需用户确认口径。**
2. **卡牌客户端 TTL + 服务端 `Cache-Control`/`ETag`**：同一张卡回看从 608 KB 降到 0/304；当前服务端统一 `no-store`。
3. **deck-detail 的 4 秒单飞等待**：`wait=4.0` 提到 8.0，或过期旧结果直接返回；可消掉 4 用户时 7.7% 的 replays 503。
4. **查清卡牌 8 秒 + details-only 降级**：另一会话已定位 `eligible_views` 占 5.9 s（两次 `stat_views` 全表扫），与这里吻合。

## 5. 边界

- 走回环 8443，不含公网 RTT 和 3 Mbps 链路的真实排队；带宽数字按字节折算，不是公网实测。
- 每阶段 90 s，缓存会变热；冷启动比例高于真实使用。
- 测试账号已 disable；token 未输出。
- 内存/CPU 只测到 4 用户；8–10 用户需下一轮（卡牌 payload 裁完再测更有意义）。

## 6. 后续：samples 已截断（2026-11-08 22:29）

按第 4 节第 1 条实施，**只改服务端，安卓端未改**：

- `statistics_portal.py`：`card-metrics` 的 `samples` 默认只留最近 **200 条**，加 `samples_truncated: true`；带 `full=1` 返回全量；
- `statistics_card_metrics.js`：网页请求加 `full=1`，来源列表仍是全量（浏览器需 Ctrl+F5，`max-age=3600`）；
- 只重启 `ygo-web`，collector/Android 均未动。

实测（`/query-api/card/45141013`，gzip）：

| 项 | 改前 | 改后 |
|---|---:|---:|
| gzip 传输 | 607,961 B | **33,452 B**（18×↓） |
| 原始 JSON | 2,112,492 B | 251,451 B |
| `samples` 条数 | 6,645 | 200（`samples_truncated:true`） |
| `by_deck` / `investment` | 有 | 仍有 |
| 网页 `full=1` | — | 全量 7,843 条，2.45 MB |

版本：portal `df6f8d50…`、js `46957a3a…`；备份 `data/config-backups/card-samples-20261108/`；回滚 `python3 /tmp/04-card-samples.py --rollback` + 重启 web。

## 7. 后续二：app 路径再省掉 investment（2026-11-08 22:47）

- 安卓端**不读** `investment`（源码与 v2.0 dex 均未见引用）；网页卡牌页用 `render(r, i)` 读它。
- 改动（只 portal）：`card-metrics` 的 `full_request = full=1 或 UA 不是 Python-urllib` 时才计算并返回 `investment`；查询接口（query_core 用 urllib）不再拿这 76 KB，也不再跑 `player_investment`。网页旧缓存 JS（不带 full）靠 UA 兜底，仍能拿到。
- 实测 portal：slim 180 KB / 带 investment 250 KB / full=1 2.45 MB；`by_deck`（App 要显示）与 `samples` 不受影响。
- 版本：portal `ffa0ce7d…`；备份 `data/config-backups/card-investment-20261108/`；回滚 `python3 /tmp/05-card-investment.py --rollback` + 重启 web。

### 但发现一个更严重的问题

改完后 query API 的卡牌返回 `metrics: null`（311 B）。原因不是 payload，而是：

| 项 | 值 |
|---|---|
| portal `card-metrics` 单次计算 | **28–48 s**（冷态三次 27/45/15 s，热一点仍 28/48 s） |
| query_core 回环超时 | **8 s**（`PortalClient(timeout=8.0)`） |
| 结果 | query_core 超时 → `card_pack` 把 `metrics` 置 null → App 显示“统计暂不可用” |

压测时 58 次卡牌只有 10 次带回 metrics、p95 8.0 s，也是同一个原因。**这是既有问题，今天冷缓存把 8 s 超时完全暴露出来；不裁 payload 也一样。** 下一步优先级应高于继续瘦身：

1. portal 给 `card-metrics` 加进程内结果缓存（按 query string，TTL 300 s）——第一次仍慢，但同一条件后续瞬间返回，App 第二次起能拿到；只改 portal、重启 web。
2. 真正的根治是把 `card_query` 从 28–48 s 降下来（另一会话已定位 `eligible_views` 占 5.9 s，还需继续拆）。
3. 或把 `PortalClient` 超时 8 s 提到 20–30 s——但要重启 collector。

## 8. 后续三：card-metrics 进程内缓存（2026-11-08 23:05）

按第 7 节第 1 条实施，只改 portal、只重启 web：

- `StatisticsPortal` 新增 `_card_metrics_cache`（按 `parsed.query`，TTL **600 s**，最多 32 条 / 12 MB，单条上限 3 MB），命中直接返回完整 reply；计算完 `_trim_heap()`。
- 实测：

| 调用 | 改前 | 改后 |
|---|---:|---:|
| portal 冷计算 | 28–48 s | **10.4 s**（invest 省略后） |
| portal 同条件第二次 | 28–48 s | **0.01 s** |
| App 卡牌（query API） | `metrics:null` / 311 B | **0.60 s / 30,212 B gzip，metrics 有值** |
| App 第二次 | — | 0.02 s |
| 网页 full=1 | 15–45 s | 6.2 s（2.48 MB，全量 7,898 条） |

- 版本：portal `17a655cd…`；备份 `data/config-backups/card-metrics-cache-20261108/`；回滚 `python3 /tmp/06-card-cache.py --rollback` + 重启 web。
- 仍存在：TTL 到期后的**第一个**请求仍会 10 s 左右，超过 query_core 8 s 超时，那一个用户会看到 `metrics:null`；后续请求命中缓存正常。要彻底消掉需降 `card_query` 耗时（28–48 s → <8 s）或把超时提到 20–30 s（重启 collector）。可先把 TTL 提到 1800 s，把“每 10 分钟一次空窗”降到每 30 分钟一次。

## 9. 根治：card_query 提速（2026-11-08 23:20）

两处、都不改输出：

1. `top100.eligible_views`：把“每局录像文件是否存在”的结果和 `stat_files→rel_path` 映射缓存 60 s（原来每次调用要做 ~8.4k 次文件 stat；有效期与 30 s 快照同级）。
2. `card_statistics.query`：只有带 `opponent_types`/`opponent_series` 时才查对手行（App 默认请求没有对手筛选，这批 1.6 万行纯浪费）。

实测（同一台机、同一张卡）：

| 口径 | 改前 | 改后 |
|---|---:|---:|
| `card_query` 热 | 6.4 s | **1.8–2.6 s** |
| `card_query` 带对手筛选 | — | 1.8 s（contains）/ 4.4 s（exact） |
| portal `card-metrics` 冷 | 10.4 s | **6.85 s**（< 8 s 超时） |
| portal 缓存命中 | 0.01 s | 0.01 s |

正确性：`candidate_samples` 9,305、`samples` 9,302 与改前一致；对手筛选仍可用（`耀圣` exact-full 404 条、contains 1,267 条、无筛选 9,305 条）。

版本：`top100.py` `438062dc…`、`card_statistics.py` `7554146d…`；备份 `data/config-backups/card-speed-20261108/`；回滚 `python3 /tmp/07-card-speed.py --rollback` + 重启 web。
边界：进程刚重启、alive 缓存为空时，第一次仍可能 7–12 s（web 启动时的 roster 预热会填上）；portal 600 s 缓存兜住后续请求。




