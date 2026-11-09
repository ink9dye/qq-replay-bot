# web 内存暴涨（RSS 60→480 MB）根因与处理（2026-11-09）

## 1. 症状

- 观察脚本数据显示：`ygo-web` 每次重启后 RSS 从 ~60 MB 在 25–180 分钟内涨到 **470–480 MB**，触发 `MemoryHigh=420M` 节流；
- 11.4 小时内 `ygo-web` 被重启 **9 次**，其中 6 次是 `memory-relief` 因系统可用内存过低（141–240 MB）主动重启；
- `memory.events high` 累计 124 万次，Swap 最高 1 185 MB；
- 每次重启 `systemd` 记录 "memory peak 480.8M, memory swap peak 602.5M"。

## 2. 根因（只读采样定位）

- 10 秒粒度采样（`/proc/<pid>/status` + `smaps_rollup` + cgroup）显示：RSS 在**一个采样间隔内**从 162 MB 跳到 **480 MB**，`Anonymous` 150→472 MB，`Private_Dirty`/`Private_Clean` 同时抬升，**子进程为 0、无 replay_analysis**；
- 跳变时刻（10:02:50）正好是进程启动后第 300 秒——`statistics_server.py` 的 `_side_loop` 每 300 秒在 **web 进程内**调用 `side_inference.run()`；
- `side_inference.run()` 会 `side_cache.load()` **整份读取** `data/cache/side-inference.json`。该文件已增长到 **171 MB**（代码注释里还是 45 MB 时代的写法），JSON→Python 对象后占用约 300 MB；
- 独立进程的 `side-inference.timer` 本来就有内存门槛（`--min-available-mb 767`）会跳过，但 web 内的这条循环没有门槛，于是每 5 分钟把进程推到 480 MB。

## 3. 改动

| 文件 | 改动 |
|---|---|
| `bot/statistics_server.py` | 删除 web 进程内的 `_side_loop`（每 300 s 的 `side_inference.run`）；批次仍由 `side-inference.timer`（独立 oneshot，`--min-available-mb 767`、`CPUQuota=50%`、`IOSchedulingClass=idle`）执行 |
| `bot/statistics_portal.py` | `PRECOMPUTED_CARD_REPORT_SECONDS` 1800 → **3600**（预计算文件 TTL 放宽到 1 小时，减少守卫跳过导致回落实时） |
| `tools/precompute_card_report.py` | 200/200 默认每 **45 分钟**才重算（`--heavy-interval 2700`），100/100 仍每轮；降低单轮 60 秒负载 |

## 4. 部署（2026-11-09 10:04）

| 项 | 值 |
|---|---|
| `statistics_server.py` | `ff02e553…` → **`72fb1abf0d56ba27`** |
| `statistics_portal.py` | `f99b6ba9…` → **`3347a499e8f4b31d`** |
| `precompute_card_report.py` | `bab0d9e2…` → **`df723e796460a0ba`** |
| 备份 | `data/config-backups/web-side-out-20261109/` |
| 重启 | `ygo-web` 10:04:56（`active_observations=0`），8788 health=200 |

## 5. 验收

| 项 | 结果 |
|---|---|
| web RSS（重启后 10 分钟） | **73–77 MB 平稳**（此前 5 分钟即 480 MB） |
| cgroup current / `memory.events high` | 87 MB / **0**（重启至今） |
| 线程数 | 4（此前 5–6，少了一条 side 循环） |
| 进程内 `cached …` 日志 | 10:04:56 之后 **0 条**（此前每 30–40 秒一条） |
| side-inference 批次 | 10:05 独立进程照常启动（可用 728 MB < 767 MB，按门槛跳过，符合预期） |
| 卡片统计预计算 | 10:13 定时器：100/100 成功 22.1 s；200/200 `skip_heavy age=863s`（45 分钟未到） |
| 页面接口 | `card-report` 100/100 **31.2 ms**、200/200 **5.1 ms**，`precomputed`，sha 与文件一致 |
| 构筑预计算 | 有效文件 65 个（`read_precomputed` 判定） |

## 6. 回滚

```bash
cp -a /opt/ygobot/data/config-backups/web-side-out-20261109/statistics_server.py /opt/ygobot/bot/
cp -a /opt/ygobot/data/config-backups/web-side-out-20261109/statistics_portal.py /opt/ygobot/bot/
cp -a /opt/ygobot/data/config-backups/web-side-out-20261109/precompute_card_report.py /opt/ygobot/tools/
cd /opt/ygobot && python3 -m py_compile bot/statistics_server.py bot/statistics_portal.py tools/precompute_card_report.py
systemctl restart ygo-web
```

## 7. 风险与限制

- web 内不再跑 side inference：批次周期由 5 分钟变为 `side-inference.timer` 的 10 分钟，且受 767 MB 内存门槛限制；内存紧张时 side 数据更新会变慢（但不会再拖垮 web）。
- 卡片统计预计算 TTL 放宽到 1 小时：最坏情况下页面显示 1 小时前的统计；页面 `generated_at` 仍是计算时刻。
- 200/200 每 45 分钟才刷新：最坏 45 分钟旧数据（与 TTL 1 小时配合，仍可命中）。
- `side-inference.json` 171 MB 本身仍是隐患（任何进程整份 load 都会很大）；后续应改为分片/SQLite 或限制条目，本次未动。
- 24 小时观察窗口跨了这次修复（第 11.5 小时处），最终报告需按“修复前/后”分段解读。

## 8. side 缓存改 SQLite（10:40 完成）

删掉 web 内循环后暴露的问题：`side-inference.service` 的 767 MB 门槛从未通过（11-08 起 196/196 次 skipped），而旧 JSON 缓存 **171 MB / 6,582 条**，整份 `json.load` 峰值 **668 MB**、13.9 s——即使放行也会把机器推进 swap。

处理：

- `bot/side_cache.py` 改为 SQLite（`data/cache/side-inference.sqlite3`，主键 build）：`load()` 返回按需视图、`get()` 单条查、`commit()` INSERT OR IGNORE，接口不变；
- `tools/migrate_side_cache.py` 流式解析旧 JSON 迁移（不整体载入），旧文件改名 `.migrated-<时间>` 保留；
- `side-inference.service` 门槛 767 → **350 MB**（`MemoryMax=600M` 保留）。

实测（10:32）：

| 项 | 结果 |
|---|---|
| 迁移 | 6,582 条，11.9 s，MemoryMax=512M 未 OOM |
| 首次批处理 | 700 considered、591 cached、91 no-side、**18 写入**、entries 6,600、**peak 145.6 MB**（原 668 MB）、300 s 到上限 `partial` |
| web | RSS 153 MB、health 200，未受影响 |

版本：`bot/side_cache.py 105874e5…`、`tools/migrate_side_cache.py 3d44f27e…`；备份 `data/config-backups/side-cache-sqlite-20261109/`。回滚见 `stability/side-cache-sqlite-20261109/README.md`。
