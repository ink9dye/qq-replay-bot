# 页面提速收尾（2026-11-06 夜 – 11-07 凌晨）

本文件收尾“网页很卡”这条线：CPU 隔离、玩家排行/饼图、卡组胜率、构筑详情、静态资源与缓存。**饼图与玩家排行的 UI 归另一条对话**（`statistics_portal.html` + `statistics_dashboard.py`），本文件记录我这边做了什么、如何与它协作、以及一次覆盖事故的修复。

## 1. 交付清单（已上线）

| 项 | 改动 | 效果 |
|---|---|---|
| CPU 隔离 | web `CPUWeight=200`；collector `CPUQuota=120%`（未重启 collector） | load 2.3–3.5 → 1.0–1.5 |
| 缓存预算 | `MAX_KEYS 16→256`、`MAX_BYTES 8MB→64MB` | 构建/名册/矩阵结果不再被挤掉 |
| 玩家排行 | `deck_images` 全表聚合从每次重跑改为**按卡组缓存 30 分钟 + 持久化到 `data/cache/deck-images.json`** | roster 冷算 22–50s → 5–8s；命中 6–9ms |
| 卡组胜率 | 200 范围先按“前100+已核验≤200”限制 SQL；页面只取 `metrics/perspectives`（`samples=0`） | 冷算 78–88s → 7–16s；页面载荷 **460KB → 94KB** |
| 构筑详情 | 单玩家视角（不再每次算完整 100 人名册）；走 QueryRefresh 持久化；4 秒等待；**绕过全局计算锁** | 3–18s → **0.45–2.3s 直接 200**（原来 4s 后 503） |
| 静态资源 | `/statistics/assets/*` `Cache-Control: private, max-age=3600`（原 no-store） | 浏览器不再每次重下 JS/CSS |
| 矩阵预热 | `prewarm` 增 matrix 100/100、200/200；构筑预热 5s/个 | 常用矩阵命中 0.03s |

## 2. 饼图与玩家排行（UI 分支协作 + 一次事故）

- **UI 归属**：`statistics_portal.html`（59KB，饼图/排行新版界面）与 `statistics_dashboard.py`（`deck_pie`、`deck_single`、`local cards` 数据）由另一条对话维护；本地卡图接口由 `statistics_local_cards.py` + `statistics_portal.py` 的 `/statistics/card-thumb/<code>`、`/statistics/card-image/<code>` 路由提供。
- **事故**：21:40 我把 p1b 的 `statistics_portal.py`（旧基线+提速改动）部署上线，覆盖了 UI 分支 19:56 的 57500 字节版本，**丢掉了上述卡图路由** → 饼图/排行的卡图 404，页面看起来“回到最初”。
- **修复**：从备份 `data/config-backups/deck-detail-refresh-20261106-214021/statistics_portal.py` 还原 UI 分支版本，只重新叠加三个与 UI 无关的提速点：
  1. `deck-detail` 纳入 QueryRefresh 且**不按查询串归一化**（玩家名不能被 parse_qs 改写）；
  2. 构筑页 4 秒等待 + 绕过全局 `_query_compute_lock`；
  3. `/statistics/assets/*` 1 小时缓存。
- **dashboard**：线上是 UI 分支 00:05 恢复的 `d85c46df`（已含 deck 图持久化与单玩家构筑）；**不再由我改动**。
- **验证**：`/statistics/card-thumb/36270527` 200（7.2KB webp，二次 8ms）、`/statistics/card-image/...` 200；饼图 3 环 6/21/46、外环 46 张图全部有 image；roster 9ms/stale；`/statistics` 页面 5ms。

## 3. 实测（2026-11-07 00:20）

| 项 | 数值 |
|---|---|
| 门户/矩阵/录像/卡片 HTML | 3–5ms（45–59KB） |
| roster API | 9ms / stale（后台计算 5–8s） |
| 矩阵 API（页面版） | 0.03s / 94KB（后台计算 7–16s） |
| 构筑页（冷玩家） | 0.45–2.3s / 200 |
| 并发 8 路混合 | 0.28s 全部 200（此前测） |
| load / available / WAL | 1.5 / 740MB / 128MB |

## 4. 版本（线上，sha256 前 16）

`statistics_portal.py e8d77d63`、`statistics_portal.html 5f3f642d`（UI 分支）、`statistics_dashboard.py d85c46df`（UI 分支）、`statistics_matrix.py 26de2d49`、`statistics_matrix.html 8b1d5146`、`statistics_prewarm.py 4c7a2245`、`statistics_server.py ff02e553`、`statistics_refresh.py 5935234f`、`top100.py 1288a888`、`card_series.py 6e5fe2b6`。

## 4.5 系列标注缓存（2026-11-07 上午，最终：仅内存缓存）

- **尝试**：ot/card_series.py 加“按构筑缓存 + 落盘 data/cache/build-series.json（约 7MB）+ 全命中快路径”。离线同库实测 roster 26.6s → 3.95s、矩阵 200/200 58.6s → 6.66s、录像 4.26s → 2.86s，输出逐项一致。
- **问题**：生产部署后 web 的 memory.high（420MB）被反复触发（10 分钟内 3000+ 次），RSS 峰值约 440MB（上限 500MB），一次 roster 计算在压力下耗到 **112s**；原因是 7MB 缓存反序列化 + annotate 的逐构筑 cards 字典 + roster 行数据在 500MB 上限内叠加。
- **最终处置**：回滚到“仅进程内缓存”的 card_series.py 6e5fe2b6，并把 uild-series.json 移到 /root/build-series.json.disabled-*（不再落盘）。当前 roster 计算 **16.9s**、web RSS **105MB**、memory.events 全 0、available 725MB。
- **结论**：这版缓存对 CPU 有效，但内存不安全；下次要上必须先做“内存有界”版本（紧凑条目 + 分块 annotate，峰值控制在 +100MB 以内），再单独开窗口验证。
- **回滚路径**：/root/last-series-persist-backup、/root/build-series.json.disabled-*。

## 5. QQ 机器人同步

- QQ 的 前百饼图 走 	op100_pie.render，数据源同样是 statistics_dashboard.query（与网页同口径），卡图优先本地卡库、其次 ygopro/pics；本次网页侧优化（deck 图缓存、单玩家）自动受益。
- QQ 侧新增：**payload 2 分钟缓存**（data/cache/top100-pie/payload.json，按库版本 + 时间），配合原有 PNG 缓存，同一数据重复请求从约 11s 降到 **9ms**；首次仍约 11s（名册计算 + 绘图）。
- 已部署 ot/top100_pie.py ad6e752d，但 **未重启 ygo-qq**（观察期，避免影响 QQ 链路）；该缓存会在下一次 ygo-qq 重启后生效。回滚：/root/top100_pie.before-cache.py。
- QQ 的 前百构筑/使用量/统计 等命令调用门户 API，门户提速后同样变快，无需改 QQ 代码。

## 6. 未做 / 下一步

- roster 计算仍有 5–8s（身份/证据部分），可做按版本缓存；用户侧已由 stale 命中保护。
- 录像筛选（p50 3.6s）与卡片统计（12s）尚未按同样思路优化。
- 前端骨架屏/并行请求未做。
- UI 分支文件（`statistics_portal.html`、`statistics_dashboard.py`）后续不擅自动；需要提速时在它们版本上打补丁并逐项对照。

## 7. 回滚

- 门户：`/root/last-portal-merge-backup`、`/root/statistics_portal.before-wait.py`；
- dashboard：`/root/last-dashboard-single-backup`、`/root/last-deck-images-backup`；
- 矩阵：`/root/statistics_matrix.py`（部署前副本）；
- CPU：`/root/ygo-web-cpu-budget.conf.bak-*`；collector `systemctl set-property ygo-collector CPUQuota=infinity`。
恢复后 `chown ygo:ygo`、`systemctl restart ygo-web`，核对 sha256 与 `/health` 200。

## 8. 提交

- 分支 `delivery/query-perf-20261106`：`ea25b2b`（夜间提速）、`1146c23`（门户还原+绕锁）、`29e4bf0`（同步线上共享文件快照）；均已推送 GitHub。
- p1b 工作树：`c52e245`、`88f420a`。
