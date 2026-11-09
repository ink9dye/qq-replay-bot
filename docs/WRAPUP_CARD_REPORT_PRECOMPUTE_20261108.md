# 卡片统计 / 构筑详情预计算上线（2026-11-08）

## 1. 背景与实测

改前（只读测量，2026-11-08 21:40–22:00）：

| 项 | 数值 |
|---|---|
| 卡片统计 100/100 计算 | 18.3 s wall / 8.1 s CPU / 峰值 +126 MB / 31.0 KB |
| 卡片统计 200/200 计算 | 38.2 s wall / 16.1 s CPU / 峰值 +99 MB / 28.3 KB |
| 预计算结果 写盘→读回→JSON 解析 | **1.7–4.2 ms** |
| 构筑详情计算 | 0.27–0.32 s；预计算文件 4–5 KB |
| 近 24 h `card-report` | 14 次请求，**11 次 503**，仅 3 次成功计算 |
| 近 24 h `build` | 16 次请求，**8 次 503** |

结论：页面慢不是算法，是「首次访问没有现成结果」；把默认视图预计算到磁盘，读取只需几毫秒。

## 2. 改动

| 文件 | 说明 |
|---|---|
| `bot/statistics_portal.py` | 新增 `card_report_precomputed_scope()`、`_precomputed_card_report()`、`_precomputed_build()`；只在原路由里**先试预计算、否则走原逻辑** |
| `tools/precompute_card_report.py` | 新增；独立进程算 100/100 与 200/200 默认视图，原子写 `data/cache/card-report-<own>-<opp>.json` |
| `/etc/systemd/system/ygobot-card-report-precompute.service` | oneshot，`Nice=10`、`MemoryMax=400M` |
| `/etc/systemd/system/ygobot-card-report-precompute.timer` | 每 15 分钟一次（`OnBootSec=5min`、`OnUnitActiveSec=15min`） |

预计算命中条件（严格）：`self_range/opponent_range` 为 100/100 或 200/200；`seat=all`、`metric=opening`、`threshold=1`、`min=0`、`sort=rate`、`partition=auto`、`limit=500`、四个 phase 齐全；不含 `code/deck/opponent/series/exclude/combo`，且不是 `phase_none=1`。其余请求全部走实时路径，行为不变。

构筑详情复用已有 `bot/query_precompute.py`：按玩家名 + 「最新录像时间」判断文件是否仍有效，不拿旧构筑冒充新数据。

## 3. 部署记录（2026-11-08 22:09–22:15）

| 项 | 值 |
|---|---|
| 改前 `statistics_portal.py` | `3073f8d61285e788`（已备份） |
| 改后 `statistics_portal.py` | `a1c18377797ed6a7` |
| `tools/precompute_card_report.py` | `bab0d9e2c72e6679`（守卫：可用内存 ≥300 MB、load ≤3.5、IO PSI<50） |
| 备份目录 | `/opt/ygobot/data/config-backups/card-precompute-20261108/` |
| 重启 | `ygo-web` 22:14 重启（`active_observations=0`），health 200 |
| 首轮预计算 | 100/100 24.6 s、200/200 59.3 s，合计 1m25s（低内存窗口用 `--force` 补算一次） |
| 定时器首轮 | 22:25:50–22:26:27 自动跑完：100/100 22.0 s、200/200 36.8 s，文件已刷新 |
| 定时器 | `ygobot-card-report-precompute.timer` active，每 15 分钟 |
| 部署后内存 | web RSS 141 MB、cgroup current 241 MB；`memory.events high` 计数随重启清零后仍会增长（历史同样如此，非本次引入） |

## 4. 验收结果（22:15）

| 请求 | 状态 | 耗时 | 头 | 备注 |
|---|---|---:|---|---|
| `card-report` 100/100 默认 | 200 | **8.7 ms** | `precomputed` | 响应 sha 与预计算文件一致 |
| `card-report` 200/200 默认 | 200 | **5.7 ms** | `precomputed` | 同上 |
| `card-report` `metric=drawn`（自定义） | 200 | 7.7 ms | `stale` | 走实时路径，未命中预计算（符合预期） |
| `build?player=灵sama` | 200 | **20.2 / 34.8 ms** | `precomputed` | 4 778 B |

定时器首轮（22:25）刷新后复测：100/100 **9.8 ms**、200/200 **6.6 ms**，响应 sha 与新文件一致。

构筑预计算：22:27 用现有 `tools/precompute_query_cache.py --limit 60` 补了 60 名玩家（1 个 busy），缓存目录 112 个文件。抽查前 5 名：`灵sama`、`珏瑜` 命中 `precomputed`（21–25 ms）；`久 远`、`亚翁` 因 22:27 之后又有新录像，按「最新录像时间」判定为过期，回落实时路径（`refreshing`，17–25 ms）；`克利西亚·米泽·凛` 无有效文件且排在队列后，4 s 后 503。**这是预期行为**：不为活跃玩家提供旧构筑；但说明构筑预计算的命中率取决于「上次预计算后该玩家是否有新录像」。

## 5. 回滚

```bash
cp -a /opt/ygobot/data/config-backups/card-precompute-20261108/statistics_portal.py /opt/ygobot/bot/statistics_portal.py
systemctl disable --now ygobot-card-report-precompute.timer
rm -f /etc/systemd/system/ygobot-card-report-precompute.service /etc/systemd/system/ygobot-card-report-precompute.timer
systemctl daemon-reload
systemctl restart ygo-web
```

`data/cache/card-report-*.json` 可留可删（不读取即无效）。

## 6. 限制与风险

- 卡片统计最多显示 **30 分钟前**的结果（文件 TTL 1800 s）；页面里的 `generated_at` 是计算时刻。
- 预计算每 15 分钟一轮；机器压力大时守卫生效会跳过，文件过期后自动回落实时计算（慢，但不会出错）。
- 300 MB / load 3.5 的守卫是本次按实际窗口调过的；若继续 OOM/swap 恶化，应回退阈值或暂停 timer。
- 只有默认参数吃预计算；筛选、单卡、组合请求行为不变。
- 构筑预计算仍由现有 `query-precompute.timer`（每日 05:34）刷新；本次 22:27 补了 60 名玩家（1 个 busy）。**活跃玩家一有新录像就判过期**（不为旧构筑冒充新数据），命中率取决于上次预计算后是否有新录像。

## 7. 后续

1. 观察 24 小时：`data/logs/card-report-precompute.log` 的 `skip_guard` 比例、`memory.events high` 增量、`card-report` 的 503 是否归零。
2. A 方案（响应缓存键去掉 WAL、TTL 60–120 s）仍在待办；本次预计算已覆盖首次打开，A 只影响“重复打开”。
3. 构筑：若要提高命中率，把 `precompute_query_cache.py` 接到更短定时器（只跑前 20–30 名 + 订阅），或对 `build` 做 stale-while-revalidate；当前活跃玩家仍会回落实时队列。

## 8. 24 小时观察（已启动）

- 服务：`ygobot-card-precompute-watch.service`（`enabled`，开机自启；`Restart=on-failure`）。
- 脚本：`/opt/ygobot/tools/watch_card_precompute.py`（`3d8c7110…`），每 300 秒采样一次，**连续按 24 小时窗口滚动**（`--windows 0`）：每个窗口结束写 `card-precompute-watch-report-w<N>-<时间>.md` 并以最新报告覆盖 `card-precompute-watch-report.md`，随后自动开始下一个 24 小时窗口；机器重启后从状态文件继续，关机时间计入当前窗口。
- 状态与数据：`data/observation/card-precompute-watch.json`（截止时间、偏移、累计）、`.jsonl`（逐样本增量）。
- 24 小时后自动写 `data/observation/card-precompute-watch-report.md` 并退出；中途可 `python3 tools/watch_card_precompute.py --status` 看当前汇总。
- 指标：`skip_guard` 比例与原因、`memory.events high` 增量（含 web 重启重置次数）、`card-report` 503 率、实时计算次数（预计算未命中）、MemAvailable 最低 / Swap 最高 / web RSS 最高，并附逐小时表。
- 停止：`systemctl disable --now ygobot-card-precompute-watch.service`（不影响预计算本身）。
3. `build` 的实时慢路径（0.3 s 计算却 2.6 s 线上）与 503 队列问题未在本次处理。
