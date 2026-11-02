# QQ 观战统计机器人 + 统计网站 功能测试报告

- 测试时间：2026-11-02 19:53—20:30（Asia/Shanghai）
- 本地代码：`D:\pythons\qq-replay-bot`（master，基线 `5f83928`）
- 线上代码：`/opt/ygobot`，公网 `101.201.108.188`
- 测试范围：统计网站、QQ 命令、健康/备份/重启运维；**查价、预定按要求未测试**
- 测试数据：`乾紗凪一番可愛`（当前第 1 名，有构筑、PT、录像）
- 写入测试前备份：`/opt/ygobot/data/backup/pre-functional-20261102.sqlite3`（371,253,248B，sha256 `34db4c8…d8fe32`）

## 1. 结论摘要

| 范围 | 结论 |
|---|---|
| 统计网站 7 个主页面 | 全部返回 200，真实浏览器渲染通过；下载、筛选、命名保存通过 |
| 统计链接安全 | 有效 200、过期/撤销 410、不存在 404、访问入口 403 通过 |
| QQ 第一批 8 条命令 | 均有回复；其中一条是用户漏写卡名的格式提示，随后补测成功；图片实际收到；发现并修复 Linux 中文缺字 |
| QQ 订阅闭环 | 完整写法通过；“只写玩家名默认全部+静默”已改并单测通过，真实 QQ 待复测 |
| 运维只读体检 | 服务/端口/内存/磁盘正常；无当前错误日志 |
| 健康巡检 | 19:25 出现一次启动窗口误报，已修复（`c61a92e`）；生产重启回归未做 |
| 备份 | 4:15 cron 已配置；手动执行成功，94MB gzip 校验通过；4:15 自然触发尚未观察到 |
| 8787 重启回归 | **阻塞**：测试时有 8 局在录，用户选择手动重启；静态/临时端口重绑通过 |
| PushPlus | **阻塞**：服务器只有 SMTP 配置，没有 `PUSHPLUS_TOKEN`；`test-notify` 通过 SMTP 发送成功一次 |

## 2. 只读体检

| 项目 | 结果 | 证据 |
|---|---|---|
| `ygo-collector` / `ygo-web` / `ygo-qq` | 均 active | `systemctl show`，MainPID 22727/20570/30756 |
| 8787 公网 `/health` | 200，0.214s | `{"status":"running","service_roles":["share","watch"]}` |
| 8788 回环 `/health` | 200，0.002s | `{"service":"qq-statistics-web","version":3,...}` |
| 8788 `/statistics` 无 cookie | 403 | 本机入口保护正常 |
| 内存 | 1.7GiB total，约 266MiB available | `free -h`；采集 8 局时 CPU 约 109% |
| 磁盘 | 40G 中已用 9.2G（25%） | `df -h /opt/ygobot` |
| 8787 监听 | `0.0.0.0:8787`，8788 仅 `127.0.0.1` | `ss -lntp` |
| 近 30 分钟错误日志 | 三服务 `-- No entries --` | `journalctl -u ... -p err` |
| SQLite | 约 356MB，WAL 约 13MB | `/opt/ygobot/data/bot.sqlite3*` |
| 卡表/卡名 | `cards.cdb`、`cache/cards.json`、`data/card-library.json` 均在 | card-library 指向 `/opt/ygobot/ygopro` |

## 3. 统计网站

### 3.1 页面与接口

| 功能 | 操作 | 结果 | 证据 |
|---|---|---|---|
| 玩家排名 | 打开 `/stats/<token>/statistics` | **通过**：200，0.104s；当前 100 人、已观测 83、未采集 17、未识别 33；饼图与榜单渲染 | `test-evidence/01-player-ranking.png` |
| 排名搜索 | `api/player-search?q=乾` | **通过**：200，0.34s，命中 1 人 | 接口 JSON |
| 卡片统计 | 打开页面；带 `#code=14558127` 查询灰流丽 | **通过**：200，0.12s 页面；查询完成“1 行”，胜率 48.8%，Wilson 区间 34.6–63.2% | `test-evidence/02-cards-report.png` |
| 卡片统计整表 | `api/card-report` | **通过**：200，137 行；33 名玩家、43 小局（灰流丽） | 接口 JSON |
| 额外卡 | 页面 + `api/cards-extra` | **通过**：200，358 行；页面显示 356 张额外卡（实时数据变化） | `test-evidence/03-cards-extra.png` |
| 卡组胜率矩阵 | 页面 + `api/matrix` | **通过**：200，49×49；单元格接口 200 | `test-evidence/04-matrix.png` |
| 录像筛选 | 页面 + `api/recordings` | **通过**：默认 2,117 个玩家小局视角，最新 2,000；筛选“前200/对手前100/主卡先攻/胜”返回 80；页面展示 25 条/页客户端分页 | `test-evidence/05-recordings.png` |
| 录像筛选 ZIP | `recordings-zip`，时间 19:00—20:10 | **通过**：200，148,181B，131 个条目 | `filtered-zip.headers` / 服务端输出 |
| 卡组命名列表 | 页面 + `api/deck-naming?scope=100` | **通过**：200，100 行；前100 已采集 83、已识别 50、未识别 33 | `test-evidence/06-deck-naming.png` |
| 卡组命名搜索 | `q=乾`、`deck=奥艺` | **通过**：各 200；1 行 / 10 行 | 接口 JSON |
| 卡组命名保存 | 对 `FH龙鸣` 先同名校验，再临时改名，再恢复原名 | **通过**：三次 POST 均 200；revision 36341→36344；最终恢复“混沌龙拳” | `/opt/ygobot/data/logs/deck-naming.log` |
| 构筑与对局 | `/build?player=乾紗凪一番可愛` | **通过**：主卡 40 张完整、额外 15 张完整、直接 Side 不完整；13 局、5 个整场组、推测 Side 7 张；PT 100 场曲线 | `test-evidence/07-build.png` |
| 构筑 YDK | `/build-ydk?...&inferred=1` | **通过**：200，648B，71 行 | 下载文件头 `#created by local statistics` |
| 整场 ZIP | `/recording-zip?player=...&match=...` | **通过**：200，3,628B，3 个 `.yrp`；中文文件名 | ZIP 条目列表 |
| 单局原始录像 | `/sample/<digest>?player=...` | **通过**：200，700B，magic `yrp2` | 下载文件头 |
| 卡图 | 构筑页图片加载 | **通过**：momobako `!thumb2` 图片实际加载；失败回退 `super-pre`；不再使用 `/statistics/card-image/` | 构筑页截图/JS |
| 中文卡名 | 构筑 API/页面 alt/title、卡片统计 | **通过**：中文卡名正常；未退回数字卡号 | 构筑页 DOM/截图 |

### 3.2 统计链接与安全

| 功能 | 操作 | 结果 | 证据 |
|---|---|---|---|
| 有效 token | 使用群内“统计”生成的链接 | **通过**：页面 200 | 截图/接口 |
| 过期 token | 临时插入已过期测试行后访问 | **通过**：410“统计链接已过期” | 自动清理测试行 |
| 撤销 token | 临时插入撤销测试行 | **通过**：410“统计链接已撤销” | 自动清理测试行 |
| 不存在 token | 随机无效 token | **通过**：404 | curl |
| 访问入口 | `/stats/<token>/statistics/access` | **通过**：403，不通过分享链接开放 | curl |
| 2 小时有效期 | 代码默认 `share.stats_link_hours=2` | **通过（代码/过期语义）**：未实际等待 2 小时；用过期行验证 410 | `statistics_proxy.py` |

### 3.3 性能观察

| 接口 | 首次/冷查询 | 说明 |
|---|---:|---|
| `recordings` | 13.4—25.3s | 1.49MB；第二次 3.3s；采集 8 局、CPU 满载时明显变慢 |
| `card-report` | 13.6—21.0s | 约 39KB；第二次仍约 16s |
| `cards-extra` | 11.2—14.2s | 约 107KB |
| `matrix` | 1.7s | 约 484KB |
| `build-detail` | 4.6—7.7s | 约 70KB |
| `deck-naming?q=` | 16—20s | 搜索时仍需扫描 |

**判定**：功能可用，但当前负载下首屏偏慢，属于已知限制；本轮未做性能重构，避免在录制高峰改查询路径。

## 4. QQ 命令

### 4.1 已实际发送并确认

| 功能 | 操作 | 结果 | 证据 |
|---|---|---|---|
| 帮助 | 私聊“帮助” | **通过**：1.005s，回复成功 | `qq-diagnostics` 20:18:47 |
| 统计 | 私聊“统计” | **通过**：0.274s，生成链接；该链接用于网站测试 | `qq-diagnostics` 20:18:52 |
| 历史 | “历史 乾紗凪一番可愛” | **通过**：首条 283ms；走势图另发成功，用户确认收到且中文正常 | `qq-diagnostics` 20:18:58/20:19:03；用户确认 |
| 构筑 | “构筑 乾紗凪一番可愛” | **通过**：文字 415ms；图片 API 一次 8.5s 超时告警，但用户确认图片实际到达 | `qq-diagnostics`；用户确认 |
| 录像 | “录像 乾紗凪一番可愛” | **通过**：887ms；分享目录 200、ZIP 200（5 个条目）、YDK 200（648B） | `qq-diagnostics`；服务端 `/s/<token>` 校验 |
| 我的订阅 | 发送“我的订阅” | **通过**：订阅列表读取正常，最终取消后为空 | `qq-diagnostics`；用户确认 |
| 订阅（完整写法） | `订阅 "乾紗凪一番可愛" 前100 静默` | **通过**：275ms，订阅成功 | `qq-diagnostics` 20:23:37 |
| 推送/静默 | 对同一订阅切换 | **通过**：回复成功 | `qq-diagnostics`；用户确认 9 条基本通过 |
| 取消订阅 | `取消订阅 "乾紗凪一番可愛"` | **通过**：约 272—297ms，最终列表为空 | `qq-diagnostics` 20:23:12/47 |
| 查卡 | “查卡 灰流丽” | **通过**：6.306s，卡图实际收到 | `qq-diagnostics`；用户确认 |
| 禁限 OCG 卡名 | “禁限 OCG 灰流丽” | **通过**：20.105s，回复成功 | `qq-diagnostics` 20:19:27 |
| 禁限表 OCG | “禁限表 OCG” | **通过**：8.088s，返回整表 | `qq-diagnostics` 20:22:40；用户确认 9 条基本通过 |

### 4.2 新语义：只写玩家名默认“全部 + 静默”

- 原实现要求 3 个参数：`订阅 玩家 全部|前100 推送|静默`；只发 `订阅 "玩家名"` 返回格式错误。
- 已改为：
  - `订阅 "玩家名"` → `全部` + `静默`
  - `订阅 "玩家名" 前100` → `前100` + `静默`
  - `订阅 "玩家名" 推送` → `全部` + `推送`
  - 兼容原三参数写法
- 提交：`fb27d88`
- 测试：`test_subscribe_defaults_to_all_silent` 等 3 项通过
- 真实 QQ：修复后未再让用户发送；**待复测**

### 4.3 未真实发送（用户选择跳过）

| 功能 | 状态 | 已有替代证据 |
|---|---|---|
| 前百饼图 | **未测/阻塞** | 服务层 `top100_pie.render` 生成 96,022B PNG，无缺字告警 |
| 使用量 灰流丽 | **未测/阻塞** | `card_usage_pie.render` 生成 76,756B PNG，无缺字告警 |
| 单卡流向 灰流丽 | **未测/阻塞** | `card_flow.render_flow` 生成 159,426B PNG，无缺字告警 |
| 前百构筑 关键词/名次 | **未测/阻塞** | 构筑页/YDK/分享链接已验证；未触发 QQ 文本分支 |
| 随机一卡 | **未测/阻塞** | 代码与单测存在，未真实发送 |
| 猜卡/揭晓猜卡 | **未测/阻塞** | 代码与单测存在，未真实发送 |
| `ck` 别名 | **部分通过** | “查卡”已验证；`ck` 仅解析器等价，未单独发 QQ |

## 5. 运维与恢复

| 功能 | 操作 | 结果 | 证据 |
|---|---|---|---|
| 8787/8788 health | curl 回环/公网 | **通过** | 见第 2 节 |
| 5 分钟巡检 | 正常执行 | **通过**：`rc=0`，无新增日志 | `python3 tools/healthcheck.py`，health.log 行数 9→9 |
| 巡检误报 | 检查历史日志 | **失败（已修）**：19:25 自动重启后 6 秒判“未运行”，19:30 才恢复；根因是固定 `sleep(5)` 短于启动时间 | `health.log` 19:25—19:30 |
| 巡检修复 | 增加 `wait_active`/`wait_port` | **通过（单元/正常执行）**：重试/超时模拟通过；正常巡检不误报；生产自动重启回归未做 | 提交 `c61a92e` |
| 告警 | `test-notify` 一次 | **部分通过**：输出 `sent`，但实际走 SMTP；`/etc/ygobot/notify.env` 只有 `SMTP_*`，无 `PUSHPLUS_TOKEN` | `test-notify` 输出 |
| 每日 4:15 备份 | 检查 cron + 手动执行 | **通过（手动）**：cron `15 4 * * * ygo /opt/ygobot/tools/backup.sh`；生成 `bot-2026-11-02.sqlite3.gz` 94MB，`gzip -t` 通过；4:15 自然触发尚未到下一次 | `/etc/cron.d/ygobot`；`data/backup/` |
| 8787 重启/EADDRINUSE | 静态 + 临时端口重绑 | **通过（模拟）**：`allow_reuse_address=True`；18887 端口关闭后立即重绑成功 | 服务端 Python 模拟 |
| 8787 生产重启 | 需要停止采集进程 | **阻塞**：测试时 `active_observations=8`；用户选择手动重启 | `capture-status.json`；用户选择 |
| QQ 重启加载字体 | `systemctl restart ygo-qq` | **通过**：20:29 启动，NapCat 已连接，三服务仍 active | `journalctl -u ygo-qq` |
| 内存/磁盘告警阈值 | 只读检查 | **通过**：磁盘 25%；可用内存约 266MB，未触发 150MB 阈值 | `free -h` |

## 6. 失败项与修复

### 6.1 健康巡检启动窗口误报

- 根因：`healthcheck.py` 在 `systemctl restart` 后固定 `sleep(5)`，而 collector 启动需要更久；19:25:01 重启、19:25:07 判失败，19:30 才恢复。
- 修复文件：`deploy/healthcheck.py`
- 提交：`c61a92e`
- 部署：
  ```bash
  scp deploy/healthcheck.py root@101.201.108.188:/opt/ygobot/tools/healthcheck.py
  ssh root@101.201.108.188 'chown root:root /opt/ygobot/tools/healthcheck.py; chmod 755 /opt/ygobot/tools/healthcheck.py; python3 -m py_compile /opt/ygobot/tools/healthcheck.py'
  ```
- 回归：`wait_active` 重试 3 次返回 True；超时返回 False；正常巡检 `rc=0`，无新误报。
- 未完成：生产 `systemctl restart ygo-collector` 的自动重启真实回归；需要空闲窗口。

### 6.2 历史 PT 走势图 Linux 缺中文

- 根因：`player_history.chart_png` 只找 `C:/Windows/Fonts/msyh.ttc`，Linux 回退到无 CJK 字体；20:19 日志出现 13 个 `missing from current font`。
- 修复文件：`bot/player_history.py`
- 提交：`1db7ce4`
- 部署：scp 到 `/opt/ygobot/bot/player_history.py`，chown，重启 `ygo-qq`（20:29）。
- 回归：直接生成图表 20,877B PNG，缺字告警 0；QQ 图片未再真实发送复测（用户确认此前图片中文正常）。

### 6.3 前百饼图/使用量/单卡流向缺中文

- 根因：`top100_pie._font`、`card_usage_pie`、`card_flow._font` 同样只找 Windows 雅黑；Linux 上中文乱码/方框。
- 修复文件：`bot/top100_pie.py`、`bot/card_usage_pie.py`、`bot/card_flow.py`
- 提交：`600793d`
- 部署：scp 三文件，chown；20:29 已重启 `ygo-qq`。
- 回归：三张图分别 96,022B / 76,756B / 159,426B，均无 `missing from current font`；真实 QQ 命令未发（用户选择跳过）。

### 6.4 订阅默认语义

- 根因：解析器硬性要求 3 个参数；用户期望 `订阅 "玩家名"` 默认 `全部 + 静默`。
- 修复文件：`bot/commands.py`、`bot/service.py`、`bot/help.py`
- 提交：`fb27d88`
- 回归：新增测试通过；已部署并重启 QQ；真实 QQ 未复测。

### 6.5 非缺陷确认

- **构筑页 Side 标注**：曾加“（推测，非直接记录）”，用户明确表示“都是推测/非直接记录，不写才是好的”，已回滚；最终页面只保留 `Side` 标题，不作为失败。
- **构筑图片发送 8.5s 超时**：日志为 `delivery_uncertain`，但用户确认图片实际到达；不盲目改发送超时。
- **`禁限 OCG` 报格式错误**：用户原文是缺卡名的 `禁限 OCG`；带卡名和 `禁限表 OCG` 均已成功。

## 7. 未解决/阻塞

1. **8787 生产重启回归**：测试时 8 局在录，用户选择手动重启。需要在 `active_observations=0` 时执行：
   ```bash
   systemctl restart ygo-collector
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/health
   ss -lntp | grep 8787
   journalctl -u ygo-collector -n 50 | grep -i EADDRINUSE
   ```
2. **PushPlus**：`/etc/ygobot/notify.env` 没有 `PUSHPLUS_TOKEN`，实际只有 SMTP；需要用户提供 token 或确认继续用 SMTP。只测试了一次 `test-notify`，不重复刷。
3. **剩余 QQ 命令**：前百饼图、使用量、单卡流向、前百构筑、随机一卡、猜卡/揭晓未真实发送；用户选择跳过。
4. **默认订阅真实 QQ**：代码/单测已通过，但修复后没有在 QQ 里重发。
5. **统计接口性能**：8 局在录时 `recordings/card-report` 可到 13—25s；未重构。
6. **2 小时自然过期**：未等待真实 2 小时，只用过期测试行验证 410。

## 8. 当前可用功能清单

### 已验证可用

- 统计网站：玩家排名、卡片统计、额外卡、卡组胜率、录像筛选、卡组命名、构筑与对局。
- 网站下载：YDK、单场 ZIP、筛选 ZIP、单局原始录像、分享目录/ZIP/YDK。
- 网站写操作：卡组命名保存（带备份、已恢复测试数据）。
- 链接安全：有效/过期/撤销/不存在、访问入口 403。
- QQ：帮助、统计、历史（图）、构筑（图+YDK）、录像（目录+ZIP）、我的订阅、完整订阅写法、推送/静默/取消、查卡、禁限 OCG 卡名、禁限表 OCG。
- 运维：8787/8788 health、备份脚本、SMTP 测试通知、QQ 重启加载。

### 源码已修复、待真实 QQ 复测

- 只写玩家名默认“全部 + 静默”（`fb27d88`）。
- 历史走势图 Linux 中文字体（`1db7ce4`，已重启 QQ，未再发历史）。
- 前百饼图/使用量/单卡流向 Linux 中文字体（`600793d`，已重启 QQ，未发命令）。

### 未验证

- 前百构筑关键词/名次、随机一卡、猜卡/揭晓的真实 QQ 回复。
- 8787 生产重启后的 TIME_WAIT/EADDRINUSE 回归。
- 4:15 自然触发的备份文件（手动已验证脚本）。

## 9. 已知限制

- 采集 8 局时 collector CPU 约 109%，服务器 1.7GB 内存仅约 266MB 可用；统计首查慢。
- Side 只根据直接记录/第 1 局基准/历史同构筑推断，当前样本直接 Side 不完整，推测 Side 7 张，不能当作完整 15 张。
- 统计链接默认 2 小时，过期/撤销返回 410，需要重新发“统计”。
- PushPlus 未配置，当前通知实际走 SMTP。
- 服务器本地 `pics` 为空是正常设计；构筑页走 momobako CDN，超先行卡回退 super-pre。
- 本地旧单测存在既有失败/环境差异（例如 `test_help_and_window` 引用不存在的 `HELP_ITEMS`、部分旧文案断言、卡图 URL 旧断言），未把旧测试结果当作本轮生产通过证据；本轮以生产接口、定向单测和真实浏览器为准。
- 工作区还有其他人未提交的统计命名/系列命名改动，本次未触碰、未提交。

## 10. 证据索引

### 截图

- `test-evidence/01-player-ranking.png`
- `test-evidence/02-cards-report.png`
- `test-evidence/03-cards-extra.png`
- `test-evidence/04-matrix.png`
- `test-evidence/05-recordings.png`
- `test-evidence/06-deck-naming.png`
- `test-evidence/07-build.png`

### 服务端日志/数据

- `/opt/ygobot/data/logs/health.log`：19:25—19:30 误报与恢复
- `/opt/ygobot/data/logs/qq-diagnostics.jsonl`：QQ 命令开始/完成/发送错误（无正文）
- `/opt/ygobot/data/logs/deck-naming.log`：测试命名 revision
- `/opt/ygobot/data/backup/pre-functional-20261102.sqlite3`：写库前备份
- `/opt/ygobot/data/backup/bot-2026-11-02.sqlite3.gz`：手动备份，94MB
- `/etc/cron.d/ygobot`：5 分钟巡检 + 4:15 备份
- `/etc/ygobot/notify.env`：仅 SMTP_*，无 PushPlus token

### 提交

| 提交 | 内容 |
|---|---|
| `c61a92e` | 健康巡检重启启动窗口误报 |
| `fb27d88` | 订阅只写玩家名默认全部+静默 |
| `1db7ce4` | 历史积分图 Linux 中文字体 |
| `600793d` | 前百饼图/使用量/单卡流向 Linux 中文字体 |

> 本文不包含任何统计 token、QQ 账号、NapCat token、SMTP 密码或 cookie。
