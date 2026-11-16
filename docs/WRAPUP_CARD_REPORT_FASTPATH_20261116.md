# 卡片统计减负与预计算保鲜（2026-11-16）

## TL;DR

- 默认页（100/100、200/200、四项 phase、无筛选）由「每次现算 1–2 分钟」改为命中预计算文件：**0.01 秒**。
- 带筛选的 200/200 单卡查询：**111.1 秒 → 46.2 秒**（同条件重查 0.00 秒）。
- 资格诊断 SQL 由 4 条合并为 2 条；卡片报表事实行 12 列压到 8 列，逐卡聚合改单遍。
- **没有建索引、没有加内存**。这两项仍是下一步的主要杠杆。

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

物理条件：库 1996MB、内存 1740MB、可用 572MB、swap 已用 627MB；`vda` 实测约 5MB/s。过期数据 0，不是库膨胀。

## 改动

| 文件 | 改动 |
|---|---|
| `bot/statistics_portal.py` | `PRECOMPUTED_CARD_REPORT_SECONDS` 3600 → **28800**（8 小时） |
| `bot/top100.py` | 资格诊断 4 条 COUNT 合并为 2 条（榜内/排除、过期/榜外） |
| `bot/statistics_card_report.py` | 事实字段 12→8 列；逐卡 `carried_rows` 不再拼 pool 列表，一趟算人数与胜负平；`fraction` 直接构造结果 |
| `bot/statistics_report_intermediate.py` | 原始表缓存版本 2→3（列数变化，旧缓存按列位 zip 会错位） |
| systemd | 新增 `ygobot-card-report-precompute-force.{service,timer}`，04:10/12:10/20:10 强制跑一轮 |

保鲜 8 小时意味着最多 8 小时旧数据：14 天窗口每天约 2200 局，8 小时增量约 2.4%，对百分比报表可忽略。接口仍返回 `X-Statistics-Saved-At`。

## 验证

**正确性（线上新旧实现同库对比，关闭原始表缓存）**

| 用例 | 结果 |
|---|---|
| 单卡 opening | SAME |
| 单卡 drawn | SAME |
| 三卡 opening | SAME |
| 组合 opening / carried | SAME |

**性能（线上实测）**

| 场景 | 改前 | 改后 |
|---|---|---|
| 100/100 默认页 | 实时 50–55s（预计算 0 命中） | **0.01s**，`state=precomputed` |
| 200/200 默认页 | 实时 111s | **0.01s**，`state=precomputed` |
| 200/200 + 单卡 | 111.1s | **46.2s**（`eligible_sql=9.6s`、`identity_read=0`） |
| 同条件重查 | — | **0.00s** |
| 预计算 100/100 | 66.1s（03:49 夜间） | 57.6s（12:55 白天） |
| 预计算 200/200 | 191.9s（03:22 夜间） | 121.2s（12:57 白天） |

`ygo-web` PID 2721974，三服务 active，8787/8788 均 200。

## 回滚

1. 备份目录：`/opt/ygobot/data/config-backups/card-report-fastpath-20261116/`
2. 覆盖 `top100.py`、`statistics_card_report.py`、`statistics_portal.py`、`statistics_report_intermediate.py`，`chown ygo:ygo`
3. `systemctl disable --now ygobot-card-report-precompute-force.timer`
4. `systemctl restart ygo-web`

## 未做

- **两个覆盖索引**（未建，需写锁 3–6 分钟）：
  - `stat_build_cards(build,partition,code,copies)`，预计 26.6s → 约 5s；
  - `stat_card_facts(digest,player,code,partition) WHERE initial_count>0`（约 29MB），预计 55s → 约 6s。
- **内存 1.7GB → 4GB**：同查询第二次仍 57.8s，说明工作集放不进页缓存；加内存是总开关。
- **每局卡号压缩小表**（约 2MB）：唯一能让「不升内存也快」的结构方案，需新表 + 回填。
