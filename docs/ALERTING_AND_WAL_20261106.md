# 告警重构与 WAL 治理（2026-11-06）

本轮把 08:18 的「可用 620MB / PSI 49.29%」误报改成“只报需要动作或已经出事”，同时处理当天暴露的 WAL 膨胀与 collector 崩溃。改动只涉及本机探活脚本和 `bot/` 两个模块；安卓端未改动。

## A. 告警重构（原 O6）

原则：有自动兜底且兜底正常的不弹窗；只弹需要人介入或已产生后果的事件；消息必须带动作与证据。

| 类别 | 判据 | 行为 |
|---|---|---|
| 保留 | 8787 + SSH banner + 对照站连续 3 次失败 / 恢复 | 邮件+弹窗 |
| 降级 | PSI `avg300 >= 40%` **且**（健康耗时 >2s 或非 200） | 才弹窗；00–08 点只发邮件 |
| 新增 | 内核 `oom_kill` 计数增加 | 弹窗，带前后计数与 `dmesg` 建议 |
| 新增 | `ygo-web/ygo-collector/ygo-qq` NRestarts 增加 | 弹窗，带服务名、最后异常行 |
| 新增 | 三服务 journal 在最近 16 分钟出现新的 Traceback | 弹窗，带异常行 |
| 新增 | WAL > 2GB 或 `/opt` 可用 < 8GB | 弹窗，带增长速率与耗尽时间估算 |
| 新增 | 每天 09:00 资源摘要（available/swap/PSI/WAL/DB/重启次数） | 只发邮件 |

- 同类冷却：PSI/WAL/磁盘 60 分钟，OOM/重启/Traceback 30 分钟；恢复通知照旧。
- 脚本：`tools/server_health_watch.ps1`（本机计划任务 `YgoServerHealthWatch` 每 2 分钟运行，内存/资源探针每 10 分钟一次，一次 SSH 取全部判据）。
- 状态文件：`data/health-watch/state.json`（旧字段自动兼容，缺失按“未知”处理，不会在升级瞬间误报）。

实测（2026-11-06 09:46）：`available=983MB swapfree=1818MB psi10=0.02 psi300=4.39 oom=0 restarts=(0,0,0) wal=6MB disk=23.1GB trace=2`，判据全部未越线，仅发每日摘要。

## B. WAL 治理与 collector 写锁崩溃

### 事故
- 09:38 `bot.sqlite3-wal` 涨到 **10.57GB**（08:20 为 6.7GB，≈3GB/h），系统盘可用降到 14GB；`bot.sqlite3` 850MB。
- 同一时段 `ygo-collector` 因 `sqlite3.OperationalError: database is locked` 两次退出（05:36:08、05:58:22，09:41–09:43 又两次），systemd 自动拉起，当时在录对局可能中断。
- 崩溃点固定在 `bot/top100.py: replace() → DELETE FROM stat_current_top100`；`service.py top100_loop` 只捕获 `OSError/ValueError/TypeError`，`sqlite3.Error` 直接打死进程。

### 处置
1. 备份：`sqlite3 .backup /opt/ygobot/data/backup/pre-wal-20261106.sqlite3`（811MB，含 WAL 内容，188s）。
2. 截断：`PRAGMA wal_checkpoint(TRUNCATE)` 首轮 `busy=1`（有长读事务）；先 `PASSIVE` 完成全部帧（`0|262881|262881`），重试即 `0|0|0`，**WAL 10.57GB → 0**，磁盘回到 24GB 可用。
3. 代码（已部署到 `/opt/ygobot`）：
   - `bot/store.py`：写连接 `busy_timeout 5000 → 30000`；新增 `journal_size_limit=134217728`，checkpoint 重置时把 WAL 截到 128MB。
   - `bot/service.py`：`top100_loop` 增加兜底 `except Exception`，记录“已跳过本轮，下轮重试”并在失败表登记 `runtime_error`，不再因单次写锁/磁盘异常退出整个 collector。

### 第二次膨胀与稳态确认（09:52）
截断后 WAL 在 5 分钟内又涨到 **1.98GB**（约 6MB/s），`PASSIVE` 只能推进到第 1381 帧——有连接从 09:47 起钉住旧快照，checkpoint 无法 reset。停 `ygo-collector` + `ygo-web` 后 `wal_checkpoint(TRUNCATE)` 一次成功（`0|0|0`），重启两服务，WAL 追到 `journal_size_limit` 后**稳定在 128MB 整**。

写入来源尚未定位：重启后 collector 的 `/proc/<pid>/io` 写入约 **10MB/s**，但 `bot.sqlite3` 大小不变、行数不变（`stat_card_facts` 1,057,320）——不是数据增长，而是同一批页被反复重写。已排除 `stat_card_facts` 整表重写：该表按 `(digest,player)` UPSERT 增量更新（`statistics_facts.py` 的 `ON CONFLICT ... DO UPDATE`）。候选是报表缓存 BLOB（`entries.payload`）或周期性刷新循环，待测量。这属 I/O 与 SSD 磨损问题，但十几 MB/s 的写入叠上长读事务就足以把 WAL 推到 GB 级——所以告警阈值从 8GB 下调到 2GB（正常稳态 128MB）。

### 部署证据
- 备份原文件：`/opt/ygobot/data/config-backups/wal-fix-20261106-1793929468/`（store.py `6332009f…`、service.py `ad1de920…`）。
- 新文件摘要：store.py `f7bed1dab2f926c8…`、service.py `d5e346bd13ff104a…`（与仓库一致）。
- 重启：`ygo-collector` 09:44:28，`NRestarts=0`，8787 health 200；`官方当前前100已刷新：09:44:29，版本7527`。
- `ygo-web` 未加载新代码（只读连接不受影响），本轮重启过一次（09:43:26）。

### 回滚
```bash
cp /opt/ygobot/data/config-backups/wal-fix-20261106-1793929468/store.py /opt/ygobot/bot/store.py
cp /opt/ygobot/data/config-backups/wal-fix-20261106-1793929468/service.py /opt/ygobot/bot/service.py
systemctl restart ygo-collector
```
WAL 截断不可逆但无数据损失（已 checkpoint 进主库，且有 811MB `.backup` 快照）；不要回滚/删除主库 WAL。

### 未解决
- WAL 为什么会涨到 10GB 的根因（长时间持有读事务的查询/连接）未定位；`journal_size_limit` 只限制重置后的文件大小，若有进程长期钉住快照，WAL 仍会增长。
- 现由探活脚本的 `WAL > 8GB` 判据兜底；`/etc/cron.d/ygobot` 04:15 备份脚本本就会尝试 `wal_checkpoint(TRUNCATE)`，但历史上一直静默失败。
- 其他后台循环若遇到 `sqlite3.Error` 仍可能退出进程，本轮只修了已复现的 top100 路径。
