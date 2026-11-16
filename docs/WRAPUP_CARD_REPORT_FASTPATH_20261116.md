# 卡片统计减负、覆盖索引与预计算保鲜（2026-11-16）

## TL;DR

- 默认页（100/100、200/200、四项 phase、无筛选）由「每次现算 1–2 分钟」改为命中预计算文件：**0.01 秒**。
- 带筛选的 200/200 单卡查询：**111.1 秒 → 26.8 秒**（首次冷缓存 50.7 秒；同条件重查 0.01 秒）。
- 100/200 全表默认条件（带筛选）现算：113.4 秒 → **46.8 秒**。
- 建了三个索引（共 246MB）并把 opening 指标改成「窄读 + NULL 抑制」；资格诊断 4 条合并为 2 条。
- **内存无法升级（服务商限制 2GB）**，因此没有走加内存路线。
- 新增两张 BLOB 摘要表（合计 **22.7MB**）：构筑卡表 0.85s、视角标记 1.87s，单卡冷查 **57.6s → 21.7s**（A/B），结果 SAME。
- 15:59 重启 `ygo-collector`，入库钩子（身份索引 + 卡片摘要）在采集进程内生效；顺带补齐旧进程漏掉的 37 条身份与 16 个 digest 摘要。

## 观测数据（为什么这么改）

| 测量 | 结果 | 结论 |
|---|---|---|
| 资格 SQL：空闲 vs 11:54 | 2.7s → **90.8s** | 写法没问题，是 I/O 供给被抢 |
| 692,926 行事实读取，连跑两次 | 54.7s / **57.8s** | 页缓存完全没热起来 |
| 构筑卡表 344,547 行 | 26.6s | 主键不含 copies，每行回表 |
| Python 合并 + 逐卡循环 | 10.4s + 20.5s | 93 万次 dict 转换、每卡扫 4 遍 |
| 今日 card-report 请求 | 78 次，**69 次 503** | 88% 失败 |
| 今日预计算 | 47 次全部 skip，最后成功 03:49 | PSI 55–83，阈值 50 |
| 预计算命中 | **0 次**（stale 6 / refreshing 3 / unknown 69） | 结果只保鲜 1 小时，04:00 起全失效 |

物理条件：库 1996MB、内存 1740MB、可用 572MB、swap 已用 627MB；`vda` 实测约 5MB/s。过期数据 0，不是库膨胀。内存无法扩容。

## 改动

| 文件/对象 | 改动 |
|---|---|
| `bot/statistics_portal.py` | `PRECOMPUTED_CARD_REPORT_SECONDS` 3600 → **28800**（8 小时） |
| `bot/top100.py` | 资格诊断 4 条 COUNT 合并为 2 条（榜内/排除、过期/榜外） |
| `bot/statistics_card_report.py` | opening 指标走 5 列窄读；逐卡聚合一趟算人数与胜负平；NULL 行参与“未知”判定 |
| `bot/statistics_report_intermediate.py` | 原始表缓存版本 2 → **4**（列集变化，旧缓存列位不兼容） |
| `stat_build_cards_cover` | `(build,partition,code,copies)`，**123MB**，消除 `SUM(copies)` 回表 |
| `stat_card_facts_opening` | `(digest,player,code,partition) WHERE initial_count>0`，**28MB**，291,097 行 |
| `stat_card_facts_unset` | `(digest,player,code,partition) WHERE initial_count IS NULL`，**95MB**，999,378 行 |
| `bot/statistics_card_summary.py` + 两张摘要表 | `stat_build_card_sets` 8.4MB、`stat_view_card_sets` 14.3MB，6 字节/条；构筑 0.85s、视角 1.87s |
| systemd | `ygobot-card-report-precompute-force.{service,timer}`，04:10/12:10/20:10 强制跑一轮 |

### 窄读的边界（必须保留 NULL 抑制）

旧口径里同一 `(digest,player)` 只要有一行 `initial_count` 为 **NULL**，合并结果就是“未知”并排除。只读 `>0` 的行会把“1 + NULL”误当成“有”，实测三卡/默认各多 20 局。做法：用 NULL 部分索引把受影响视角找回来（约 27 万行 / 1.3 秒），按卡号+分区存成视角编号集合，逐卡只弹出受影响的键，不扫全表。

## 验证

**正确性（线上同库、同一读事务快照、关闭原始表缓存）**

| 用例 | 结果 |
|---|---|
| 单卡 opening | SAME |
| 单卡 drawn | SAME |
| 三卡 opening（双向各跑一次） | SAME |
| 组合 opening / carried | SAME |
| 默认全表 opening（双向各跑一次） | SAME |

**性能（线上实测）**

| 场景 | 改前 | 改后 |
|---|---|---|
| 100/100 默认页 | 实时 50–55s（预计算 0 命中） | **0.01s**，`state=precomputed` |
| 200/200 默认页 | 实时 111s | **0.01s**，`state=precomputed` |
| 200/200 + 单卡 | 111.1s | **26.8s**（首次冷缓存 50.7s） |
| 100/all 全表 opening | 113.4s | **46.8s** |
| 同条件重查 | — | **0.01s** |
| 单卡冷查（摘要 A/B） | 57.6s | **21.7s** |
| 组合冷查（摘要 A/B） | 45.5s | **23.5s** |
| 构筑卡表读取 | 26.6s | **4.32s** |
| opening 事实读取 | 55s（692,926 行） | **0.14s**（80,279 行，索引内完成） |
| NULL 抑制查询 | — | 1.33s（271,890 行） |

建索引耗时：`stat_build_cards_cover` 56.3s、`stat_card_facts_opening` 70.9s、`stat_card_facts_unset` 86.0s；均在 I/O 压力 5.7 的窗口内完成，读请求未中断。库 1996MB → 2211MB，磁盘剩余 16GB。

`ygo-web` PID 2732531，三服务 active，8787/8788 均 200。

## 入库钩子生效：重启 collector（15:59）

摘要与身份两份钩子都写在 `statistics_facts.import_replay` 里，但采集进程 PID 2691371 是 10:34 起的，一直跑旧代码。15:59:20 在 `capture-status.active_observations=0`、`updated_at` 仅 6 秒新鲜时重启 `ygo-collector`：**2691371 → 2753518**，`ygo-web` 2741179、`ygo-qq` 2706023 不动，8787/8788 均 200，启动日志无 Traceback（本进程是 server-only，只发现/派发，在录会话由远程客户端继续）。

**重启前后核对**

| 项 | 结果 |
|---|---|
| 重启后入库 12 条小局 | `stat_views` 条数 == `stat_view_card_sets` 条数，所属构筑在 `stat_build_card_sets` 全部命中，**0 处不一致** |
| 重启后收到录像 | 16:00:23 起 6 个 `.yrp`，对应 digest 摘要与身份行均已写入 |
| 旧进程漏账（身份） | `backfill_match_identity.py` 扫出 37 条，0.6 秒补齐，抽查 10 条 **0 条**与现算不一致 |
| 旧进程漏账（摘要） | 16 个 digest 缺视角摘要，`refresh_digest` 补 0.3 秒，**missing 0** |

漏账成因：11:48 上线身份索引、14:35 上线摘要时都只重启了 `ygo-web`，采集进程仍是旧代码；这 4 小时内由采集入库的录像不会写表，查询靠「未索引回退」和「缺失键局部回退」兜底，所以只是慢、不影响口径。

**遗留**：身份索引只有手工回填工具，没有定时器（摘要那套有 04:40 的 `ygobot-card-summary-backfill.timer`）。只要还有进程跑不含钩子的代码，缺口就会重新累积；建议下一轮给 `backfill_match_identity.py` 加一份每日定时（增量 37 条只需 0.6 秒，全量 3 万条 9.7 秒）。

## 回滚

1. 备份目录：`/opt/ygobot/data/config-backups/card-report-fastpath-20261116/`（前端前一轮）与 `card-report-indexes-20261116/`（本轮两个 py）
2. 覆盖 `statistics_card_report.py`、`statistics_report_intermediate.py`，`chown ygo:ygo`
3. 如需彻底回退索引：`DROP INDEX stat_build_cards_cover; DROP INDEX stat_card_facts_opening; DROP INDEX stat_card_facts_unset;`（旧代码不依赖它们）
4. `systemctl disable --now ygobot-card-report-precompute-force.timer`
5. `systemctl restart ygo-web`
6. 15:59 这次只重启了采集进程、没有改代码，**本身无需回滚**；若要撤掉入库钩子，按上面两步恢复 py 后 `systemctl restart ygo-web ygo-collector`（旧代码不读两张摘要表，可保留）。

## 未做

- **内存 1.7GB → 4GB**：服务商限制，无法升级，已确认放弃。
- 紧凑摘要已完成：回填 36,366 副构筑 / 61,292 个视角，22.7MB，抽检 60 条 0 不一致；夜间 04:40 回填定时器。
- 仍剩资格 SQL 约 9 秒（4.1–12.2 秒波动）与身份继承约 5 秒；要压到 10 秒内需缓存 eligible 快照，属下一阶段。
