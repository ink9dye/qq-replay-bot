# 收尾：2 GB 内存治理 + 安卓查询性能（2026-11-07 14:26 — 11-08 23:20）

## 1. 起点

- 机器固定 2 vCPU / 2 GB，**不能升配**；当时 web 439 MB 常驻 + 369 MB 换出、`memory.high` 事件 6 万、swap I/O 3–4 MB/s、iowait 73–81%。
- 安卓查询在 1 个用户时就有 16.7% 的 503，2 个用户 44.3%；roster 计算 p50 30.5 s，全程占 `_query_compute_lock`。
- 约束：不动安卓端程序；统计口径/算法不擅自改；线上重启要挑窗口。

## 2. 四步改动

### A. 内存腾挪（11-07 14:26）

| 项 | 内容 |
|---|---|
| drop-in | `ygo-web.service.d/zz-mem2g.conf`：`MemoryHigh=480M`、`MemoryMax=560M`、`OOMScoreAdjust=200`；collector `-500` |
| zram | 1 GB lz4、prio 100，保留 2 GB `/swapfile` |
| 效果 | available 157 → 400–600 MB；swapfile 683 → 643 MB（后续 481 MB）；si/so → 0–0.5 MB/s；0 OOM、0 重启 |
| 踩坑 | `set-property` 后 `daemon-reload` 会被 `memory.conf` 改回 420/500；改 `zz-` drop-in 才生效。`OOMScoreAdjust` 只能写 `/proc/<pid>/oom_score_adj` |

### B. roster 预计算（11-07 15:00）

- 新增 `tools/roster_precompute.py` + `ygobot-roster-precompute.timer`（4 分钟、nice 10），写 `data/cache/roster.json`；
- portal `_roster_reply` 优先读该文件（≤900 s），`keep_roster_warm` 也随之变成读文件；另有 `_trim_heap()`；
- 效果：15:00 后 web 的 roster 计时 **0 条**；15:00–20:50 查询 503 共 **7 次**，全部是 deck-detail，无 roster 相关（对照 11 点 34 次/107）；
- 另一会话的 `deck-pie.png` 路由也调 `_roster_reply()`，自动复用预计算。

### C. 卡牌 payload 瘦身（11-08 22:29 / 22:47）

| 改动 | 效果 |
|---|---|
| `metrics.samples` 默认截到最近 200 条 + `samples_truncated`；网页 `full=1` 保留全量 | 单次卡牌 gzip **607,961 → 33,452 B**（18×） |
| app 路径不再返回 `investment`（安卓不读；网页按 `full=1`/浏览器 UA 保留） | slim 180 KB vs 250 KB；`by_deck`/`samples` 不变 |

### D. card-metrics 缓存 + card_query 提速（11-08 23:05 / 23:20）

| 改动 | 效果 |
|---|---|
| portal `_card_metrics_cache`（按 query，TTL 600 s，32 条/12 MB） | 冷 10.4 s → 命中 0.01 s |
| `top100.eligible_views` 缓存“文件是否存在”+ `stat_files→rel_path` 60 s（原每次 ~8.4k 次 stat） | |
| `card_statistics.query` 无对手筛选时跳过 ~1.6 万行对手查询 | `card_query` 6.4 → **1.8–2.6 s** |
| 合计 | portal 冷 10.4 → **6.85 s**（低于 query_core 8 s 超时）；App 卡牌 **0.05 s / 30,696 B / metrics 有值** |

## 3. 验证

- **压测（11-07 22:26，1/2/4 用户 × 90 s）**：4 用户 156 请求、3×503（replays 单飞）、出口 10.53 MB/90 s = 0.94 Mbps；available 387–648 MB、web ≤429 MB、load ≤3、PSI ≤8，未触发中止。卡牌占出口 92.5%（608 KB/次），是当时唯一带宽大头。
- **正确性**：`candidate_samples` 9,305 / `samples` 9,302 与改前一致；对手筛选 exact-full 404、contains 1,267、无筛选 9,305；卡牌响应 `by_deck` 93（默认范围）、`samples` 200、无 `investment`。
- **端到端**：query API 卡牌 0.60 s / 30,212 B（修缓存后）、0.05 s（提速后），均带 metrics。

## 4. 线上版本（sha256 前 16）

| 文件 | 版本 |
|---|---|
| `bot/statistics_portal.py` | `17a655cd` |
| `bot/statistics_card_metrics.js` | `46957a3a` |
| `bot/top100.py` | `438062dc` |
| `bot/card_statistics.py` | `7554146d` |

备份目录：`data/config-backups/card-samples-20261108/`、`card-investment-20261108/`、`card-metrics-cache-20261108/`、`card-speed-20261108/`、`mem2g-20261107/`。

回滚命令见 `stability/mem2g-20261107/` 各脚本 `--rollback`；A 用 `01-memory.sh --rollback`，B 用 `03-apply.py --rollback` + 停 timer。

## 5. 未解决与风险

1. **card-metrics 冷启动窗口**：TTL 到期/进程刚重启时第一次仍可能 7–12 s；web 启动的 roster 预热会填上，portal 600 s 缓存兜后续。要彻底消掉需把 `card_query` 再降到 <3 s 或把 `PortalClient` 超时提到 20–30 s（需重启 collector）。
2. **deck-detail 单飞 4 s 等待**：4 用户时 7.7% replays 503，尚未改。
3. **带宽**：3 Mbps 仍未升；卡牌瘦身后卡牌流量降 18×，新的出口大头回到 `by_deck`（~110 KB raw / 十几 KB gzip）。升带宽触发线不变（峰值 >1.5 Mbps、单日 >10 GB）。
4. **代码归属**：`statistics_portal.py` 同时有另一会话的 `deck-pie`/`card-report` 预计算改动，本收尾只记录自己这部分；他们再部署时需保留 `MERGE_FOR_PIE.md` 里的 6 处钩子。
5. **PROJECT_HANDOFF** 已有逐条记录（11-07 11:50 起），本文件是汇总版。

## 6. 证据

| 内容 | 位置 |
|---|---|
| 压测逐请求 | `workflow-results/stress-20261107-2230/stress.json` |
| 压测结论 | `docs/STRESS_RESULT_20261107.md` |
| 容量/带宽分析 | `docs/STRESS_ANDROID_20261107.md` |
| 补丁包与脚本 | `stability/mem2g-20261107/`（`01-memory.sh`、`02-roster-precompute/`、`03-apply.py`、`04-card-samples.py`、`05-card-investment.py`、`06-card-cache.py`、`07-card-speed.py`、`query_stress.py`） |
| 合并说明 | `stability/mem2g-20261107/MERGE_FOR_PIE.md` |
