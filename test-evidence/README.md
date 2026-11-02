# QQ 观战统计功能测试证据

采集时间：2026-11-02 19:53—20:30（Asia/Shanghai）
线上：`101.201.108.188`，代码 `/opt/ygobot`，统计页 8787，统计后台 8788。
测试账号/玩家：`乾紗凪一番可愛`（当前第 1 名，有构筑、PT、录像）。
查价、预定相关功能按用户要求未测试。

## 截图

| 文件 | 页面 |
|---|---|
| `01-player-ranking.png` | `/statistics` 玩家排名、饼图、榜单 |
| `02-cards-report.png` | `/statistics/cards-report` 查询“灰流丽”后的结果 |
| `03-cards-extra.png` | `/statistics/cards-extra` 额外卡统计 |
| `04-matrix.png` | `/statistics/matrix` 卡组胜率矩阵 |
| `05-recordings.png` | `/statistics/recordings` 录像筛选 |
| `06-deck-naming.png` | `/statistics/deck-naming` 卡组命名列表 |
| `07-build.png` | `/statistics/build?player=...` 构筑、PT、卡图、对局与下载 |

截图使用无头 Edge 采集，页面正文和图片均已渲染；截图中不显示 token。

## 网站接口抽样（公网 8787；时间包含公网传输）

| 接口 | 状态 | 首次耗时 | 大小 |
|---|---:|---:|---:|
| `api/roster` | 200 | 1.07s | 32,296B |
| `api/build-detail?player=...` | 200 | 4.59s | 69,889B |
| `api/recordings` | 200 | 13.4–25.3s | 1.49MB |
| `api/card-report` | 200 | 13.6–21.0s | 38,900B |
| `api/cards-extra` | 200 | 11.2–14.2s | 107KB |
| `api/matrix` | 200 | 1.70s | 484KB |
| `api/deck-naming?scope=100` | 200 | 4.93s | 30,599B |
| `api/pt-history?player=...` | 200 | 1.44s | 16,432B |
| `api/player-search?q=...` | 200 | 0.34s | 384B |

首轮测量时 collector 有 8 局在录，进程 CPU 约 109%，服务器可用内存约 266MB；慢主要是负载和冷查询，不能当作页面永久固定耗时。第二次 recordings 为 3.3s；card-report 仍约 16s。

## 下载与写库

- 构筑 YDK：200，648B，71 行，Content-Type `text/plain`。
- 单场整场 ZIP：200，3,628B，3 个 `.yrp` 条目，Content-Disposition 为中文玩家名。
- 录像筛选 ZIP：200，148,181B，131 个条目，时间范围 2026-11-02 19:00—20:10。
- 原始样本：200，700B，magic `yrp2`。
- 分享页：`/s/<token>` 200（7,330B）；`/s/<token>/zip` 200（5,961B，5 个条目）；`/s/<token>/build-ydk` 200（648B）。
- 卡组命名 POST：同名校验、临时改名、恢复原名均 200；最终恢复 `混沌龙拳`，revision 36341→36344。
- 写入前备份：`/opt/ygobot/data/backup/pre-functional-20261102.sqlite3`，371,253,248B，sha256 `34db4c8feaa8908eeda965c5951b0b488d47710a891a23d5459cbd6e90d8fe32`。

## 运维

- 三服务 active；8787 公网 `/health` 200，8788 回环 `/health` 200。
- 8788 `/statistics` 无 owner cookie：403；公开入口 `/statistics/access`：403。
- 统计 token：有效 200；人为过期 410“统计链接已过期”；撤销 410“统计链接已撤销”；不存在 404。
- 健康巡检正常执行 `rc=0`，无新增误报；历史误报行见报告。
- 告警测试执行一次：`test-notify` 输出 `sent`；实际配置只有 SMTP，无 `PUSHPLUS_TOKEN`。
- 备份脚本手动执行成功：`/opt/ygobot/data/backup/bot-2026-11-02.sqlite3.gz`，98,252,719B，gzip 校验通过。
- 8787 端口复用：`ReplayHTTPServer.allow_reuse_address=True`，临时端口 18887 立即重绑成功；生产 collector 重启未执行（8 局在录，用户选择手动）。
- QQ（2026-11-02 20:29 重启后）：`ygo-qq` active，NapCat 已连接；字体修复已随进程加载。

## QQ 命令日志抽样

来源：`/opt/ygobot/data/logs/qq-diagnostics.jsonl`（不含消息正文和 token）。

| 时间 | 操作 | 结果 |
|---|---|---|
| 20:18:47 | 帮助 | reply success，1005ms |
| 20:18:52 | 统计 | reply success，274ms |
| 20:18:58 | 历史 | reply success，283ms；另发图成功 |
| 20:19:03 | 构筑 | reply success，415ms；图片 API 在 20:19:43 超时 8516ms，用户确认图片到达 |
| 20:19:09 | 录像 | reply success，887ms |
| 20:19:15 | 查卡 | reply success，6306ms |
| 20:19:27 | 禁限 OCG 卡名 | reply success，20105ms |
| 20:22:40 | 禁限表 OCG | reply success，8088ms |
| 20:23:12/47 | 取消订阅 | reply success，约 270–300ms |
| 20:23:24 | error（修复前的“订阅 玩家名”缺省写法） | 返回格式错误；已在 `fb27d88` 修复 |
| 20:23:37 | 订阅（完整写法） | reply success，275ms |

用户确认：构筑图、历史走势图、查卡图均实际收到；历史图中文正常。
