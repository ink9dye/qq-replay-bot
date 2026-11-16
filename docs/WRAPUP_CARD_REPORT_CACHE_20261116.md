# 卡片统计缓存被采集写库击穿 + 文件存活判定收尾（2026-11-16）

> 结论：卡片统计慢/频繁 503 的主因不是“卡选多了”，而是三处每查必算的重复劳动：结果缓存键绑了整库 WAL 物理状态（20 秒即失效）、文件存活判定 60 秒过期、以及采集持续写入让预计算几乎从未跑成。本轮按 A+C 两档修：缓存改时间桶、存活判定分正负 TTL。已部署并重启 web，实测同条件第二次 0.00 秒返回。

## 0. TL;DR

| 项 | 结果 |
|---|---|
| 现象 | 卡片统计点查询后长时间转圈；观测窗口内 card-report 请求 66 次仅 5 次成功（92% 失败），24h 窗口 96 请求 81 失败 |
| 根因 A | `statistics_report_intermediate.response_identity` 把 `bot.sqlite3` 与 `-wal` 的 `mtime_ns/size` 写进缓存键；实测 WAL **20 秒内必被采集写入** → 每次计算（最长 544 秒）的缓存 20 秒即作废 |
| 根因 C | `top100._ALIVE_TTL = 60`：录像文件存活判定 60 秒过期，一次查询要重做 2.8 万次 `stat`（io_pressure 80 时 44.8 秒） |
| 根因 D（未修） | `card-report-precompute` 每 15 分钟一次，08:33 起每轮都被 `skip_guard`（io_pressure 55–83 / load 4–6）跳过，等于从未预热 |
| 修复 A | 缓存键改成「时间桶（600 秒）+ 配置文件/首局确认库物理状态」；`response_get` 不再把 TTL 压到 10 秒；card-report 传入 `ttl=600` |
| 修复 C | 存活判定拆正负：文件在信任 1800 秒，判不在仍 60 秒（避免录像文件晚到被人为排除半小时）；路径索引仍 60 秒 |
| 版本 | 未提交（`bot/top100.py` 工作树含另一会话未上线的 `reuse_context` 候选，不能整文件提交） |
| 部署 | 4 个 py 文件，备份 `data/config-backups/card-report-cache-20261116-1136/`；web 2701349→2706992，8787/8788 健康 200 |
| 实测 | 同条件两次请求：第一次后台 55.3 秒算完，之后两次均 **0.00 秒**命中（旧实现 20 秒后就作废） |

## 1. 证据

`data/observation/card-precompute-watch.json`（当轮 24h 窗口，153 次采样）：

| 指标 | 值 |
|---|---|
| card 请求 / 成功 / 失败 | 66 / 5 / **61（92%）** |
| 预计算 成功 / 跳过 | 22 / 49（io_pressure 24、memory_low 17、load_high 8） |
| MemAvailable 最低 | 174 MB（总内存 1740 MB） |
| Swap 最高 | 1813 MB |
| web RSS 最高 | 316 MB |
| memory.events high 增量 | 7991（期间 web 被 memory-relief 重启 2 次） |

`statistics-query-timings.jsonl` 里那次 544 秒的 card-report：

| 阶段 | 耗时 |
|---|---|
| `identity_read_ms`（读 stat_evidence 的 capture JSON 判首局身份） | 177.9 s |
| `eligible_source_ms`（文件存活检查，28,343 次、缓存 0 命中） | 44.8 s |
| `eligible_sql_ms` | 4.8 s |
| `source_index_ms` | 5.4 s |
| 其余（卡牌事实/组合聚合，无细分埋点） | ≈311 s |

缓存为什么等于没有：`response_identity` 取 `bot.sqlite3` 与 `bot.sqlite3-wal` 的 `st_mtime_ns/st_size` 入键，`response_get` 再取 `min(10, ttl)`。实测 WAL 20 秒内必变 → 键必变 → 磁盘中间层与响应缓存双双失效。

## 2. 改动

| 文件 | 改动 |
|---|---|
| `bot/statistics_report_intermediate.py` | `response_identity` 去掉库/WAL 物理状态，改为 `RESPONSE_BUCKET_SECONDS=600` 时间桶 + 4 个配置 json + 首局确认库/上传会话库物理状态；`response_get` 的 `max_age` 改为 `min(MAX_AGE, ttl)`；`MAX_AGE` 300→900 秒 |
| `bot/statistics_portal.py` | 新增 `CARD_REPORT_CACHE_SECONDS = 600.0`，card-report 调用传入 `ttl` |
| `bot/top100.py` | 新增 `_ALIVE_VERDICT_TTL = 1800.0` 与 `_alive_verdict_ttl(ok)`：文件在 1800 秒、判不在 60 秒；`_ALIVE_TTL`（路径索引）保持 60 秒 |
| `bot/statistics_card_report.py` | 组合上限 `MAX_COMBO_DEFS = 99`（配合前端放开 4→99） |
| `tests/test_card_report_cache_identity.py` | 新增 7 项：WAL 写入不失效、命名配置仍失效、非 card-report 不缓存、桶上限、600 秒复用、TTL 仍会过期、存活判定正负 TTL |
| `tests/test_card_report_combo_limit.py` | 新增 4 项：99 可通过、100 被拒、单组合 2–8 张规则不变 |

## 3. 验证

- 本机：两个新增测试文件 13 项全过；`tests.test_query_integration` 等既有失败经比对为**既有问题**（`bot/service.py` 缺 `_bind_pending`、`covered_players 0!=1`，把 `_ALIVE_VERDICT_TTL` 临时改回 60 也不通过），非本轮引入。
- 服务器 Python 3.12：四个文件 `py_compile` 通过；`parse_combos` 直调：99 个通过、100 个报「一次最多设置 99 个组合。」
- 线上：重启 `ygo-web` 2701349→2706992，`8788/health` 200，`ygo-qq`/`ygo-collector` 未动。
- 效果：`/statistics/api/card-report?code=14558127&self_range=100&opponent_range=all` 首次后台计算 55.3 秒（`src 2.7s`、`id 6.9s`），随后两次请求均 0.00 秒返回同一份 `generated_at`。

## 4. 回滚

备份目录 `/opt/ygobot/data/config-backups/card-report-cache-20261116-1136/`（4 文件，含原 rawSHA：`aac2c80dc2d29fb1`(top100)、`3920ace9e0ec9946`(portal)、`58b0d4ec724a654a`(intermediate)、`b4cfd908eb5a201e`(card_report)）。恢复后 `chown ygo:ygo`、`py_compile`、授权重启 `ygo-web` 即可；不涉及数据库。

## 5. 还没做的（治本下一步）

1. **身份继承落表**：`statistics_match_identity._read_capture_metadata` 每次请求重读 `stat_evidence.payload_json`（178 秒）。应入库时算一次（session_id、game_no、首局 anchor、双方卡组），查询只读小表；历史 1.5 万视角离线回填一次。预计 178 秒 → 数十毫秒。
2. **预计算真正跑起来**：现在每轮都被 `skip_guard` 跳过；改为低峰期强制跑、或把 precompute 与用户查询分队列。
3. **容量**：1740 MB 内存 + 1813 MB swap、PSI io 最高 81.5%，任何算法优化都在这台机器上打折。加内存/换 4 核是最直接的杠杆。
4. **提交**：`bot/top100.py` 工作树混有另一会话的 `reuse_context` 候选，本轮只把该文件的“存活 TTL”补丁单独落到线上，未做 git 提交。
