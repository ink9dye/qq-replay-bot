# 统计页面整体提速（2026-11-06 晚，生产已上线）

本文件记录 2026-11-06 傍晚到夜间的一轮“网页很卡”治理：CPU 隔离、缓存扩容、玩家排名/卡组胜率/构筑详情提速与预存。所有改动已部署并实测，未改统计口径与证据规则。

## 1. 根因（实测）

| 项 | 数据 |
|---|---|
| 负载 | load 2.3–3.5（2 vCPU 饱和） |
| CPU | collector 79.6%（无 CPU 配额）、replay_analysis 76%、web 仅 10% |
| cgroup | web CPUWeight 50（collector 默认 100）→ 高负载时 web 只分到约 1/3 |
| roster | 每次刷新 22–50s，其中 `deck_images` 一条全表聚合 **15.05s** |
| 缓存 | last-success 仅 8MB / 16 key，算完容易被淘汰 |

## 2. 改动与版本

| 文件 | 改动 | sha256 前 16 |
|---|---|---|
| `systemd drop-in` `/etc/systemd/system/ygo-web.service.d/cpu-budget.conf` | CPUWeight 50→200 | — |
| collector cgroup（set-property） | CPUQuota=120%（未重启 collector） | — |
| `bot/statistics_refresh.py` | MAX_KEYS 16→256、MAX_BYTES 8MB→64MB | `5935234f908f013f` |
| `bot/statistics_dashboard.py` | `deck_images` 按卡组名缓存 30 分钟 | `84d126fb6ddc406c` |
| `bot/top100.py` | 200 范围先按“前100 + 已核验≤200”限制 SQL（≤60% 才启用，否则原路径） | `0fd37ea2a3835cd2` |
| `bot/card_series.py` | 系列标注按构筑缓存（版本+完整度） | `6e5fe2b68af07a6b` |
| `bot/statistics_portal.py` | `deck-detail` 纳入 QueryRefresh，且不按查询串归一化 | `baab169823c64157` |
| `bot/statistics_prewarm.py` | 新增 matrix 两个 key + 构筑预热（15s/人，忙/Side/内存 550MB 让路） | `4c7a2245529a810a` |
| `bot/statistics_server.py` | 预热循环改 5s tick：300s 一个范围 key、其余 tick 一个构筑 | `1b46b1993f752265` |

## 3. 实测效果

### 热态（最近被预热/访问过）

| 页面 | HTML | 数据接口 |
|---|---:|---|
| 前100 门户 | 3ms（45KB） | roster 5ms / stale |
| 卡组胜率 | 3ms（26KB） | matrix 100/100 21ms；200/200 20ms |
| 录像筛选 | 3ms（18KB） | 默认 50ms（871KB，refreshing） |
| 卡片统计 | 3ms（8KB） | 默认 8ms / stale |
| 构筑详情 | — | 已预热玩家 4–6ms / stale |

并发 8 路混合（matrix 100/200、roster×2、录像、构筑×2）：墙钟 0.28s，全部 200，最慢 263ms。

### 冷态（从未查过的条件）

| 条件 | 首次 | 重试 | 端到端 | 之后 |
|---|---|---|---:|---|
| 录像 + 日期/胜负 | 503（1.0s，query_busy） | 1 次 | 6.0s | 200 fresh，已缓存 |
| 矩阵 + 先攻 | 503（1.0s） | 1 次 | 6.0s | 200 fresh |
| 卡片 + 阈值/排序 | 503（1.0s） | 1 次 | 6.0s | 200 fresh |
| 冷玩家构筑 | — | 0 | 5ms（refreshing） | 200 |

冷态 6 秒中约 5 秒是 `Retry-After: 5` 的等待，实际计算 1–4 秒；拿到一次后持久缓存。

### 资源

web RSS 199–207MB；available 750–777MB；WAL 128MB（上限）；swap free 1589MB；load 1.1–1.7（改前 2.3–3.5）。

## 4. 语义与边界

- 未核验排名仍不计入；200 资格规则、`filter_rows` 裁决不变；固定名次状态对照下矩阵 cells/total/rows/columns 完全一致，roster people/summary 完全一致。
- 缓存命中仍是带 `saved_at` 的 **stale**，后台刷新，不标 fresh；构筑详情 TTL 5 分钟、last-success 30 分钟可读、跨重启。
- 构筑预热只覆盖“最近 5 分钟内被轮到”的玩家（100 名轮一圈 25 分钟），不是全量常驻；做到全量 5 分钟新鲜需要约 1 个/3 秒（≈100% worker），不可行。
- 名次持久化未做：SQL 归因显示瓶颈是 `deck_images`（已缓存），名次网络核验占比很小；持久化会改动 120s 资格时效语义，故不做。

## 5. 回滚

| 项 | 备份 |
|---|---|
| web CPU 权重 | `/root/ygo-web-cpu-budget.conf.bak-*`（改回后 `daemon-reload` + 重启 web） |
| collector 配额 | `systemctl set-property ygo-collector CPUQuota=infinity` |
| roster 两文件 | `/root/last-roster-cache-backup` |
| 构筑预热/portal | `/root/last-build-prewarm-backup`、`/root/last-deck-detail-backup` |
| 矩阵/系列 | `/root/last-matrix-roster-backup` |

恢复对应文件后 `chown ygo:ygo`、`systemctl restart ygo-web`，核对 `sha256sum` 与 `/health` 200。

## 6. 证据

`workflow-results/statistics-query-profile-20261106/`：`roster_sql_profile.py`（15.05s 定位）、`roster-cache-compare-result.jsonl`（37.4/20.5s → 6.95/4.54s）、`matrix-fixed-equivalence3.json`（等价性）、`build_refresh_check.py`（持久缓存验证）、`web-perf-now.json`、`web-perf-cold.json`（页面实测）。
