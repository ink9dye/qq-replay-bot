# 收尾：QQ「当月历史」指令（2026-11-08）

## 1. 需求与口径

- 新增 `当月历史 玩家名`：按**北京时间当月**统计场次、胜率、当前分数/排名，并出当月积分走势图（0 = 当月首场赛前积分）。
- 原 `历史 玩家名` 保留，帮助文案改为“最近竞技战绩与积分走势图”（最近 100 局，APK 口径）。
- 数据源仍为官方公开竞技历史接口（`https://sapi.moecube.com:444/ygopro/arena/`），不依赖订阅/录像；不读本地统计库。

## 2. 改动

| 文件 | 内容 |
|---|---|
| `bot/commands.py` | `head in ("历史","当月历史")`，`当月历史` 额外置 `monthly=True`；错误提示按实际命令名 |
| `bot/service.py` | `op == "player_history"` 且 `monthly` 时走 `handle_monthly`；等待提示改为“当月历史查询中…” |
| `bot/player_history.py` | 新增 `monthly_stats()`（按北京时间月初切）、`monthly_chart_png()`（0 为首场赛前，标注首尾分值）、`handle_monthly()`、`_month_rows()`（分页取数、拒收不完整页）；复用原有缓存/旧结果兜底 |
| `bot/help.py` | 新增 `当月历史 玩家名：当月场次、胜率、当前分数/排名和积分走势图（0 为首场赛前积分）`；`历史` 文案改为“最近竞技战绩” |
| `docs/FEATURES.md` | 命令表补 `当月历史`，`历史` 口径改为“最近 100 局” |
| `tests/test_player_history.py` | 新增/调整 10 项：解析、月度边界、分页停止、拒收不完整页、无对局/接口失败文案、帮助、鉴权、PNG 投递不泄露 base64 等 |

## 3. 行为

- `当月历史 玩家名`：先回“当月历史查询中，完成后把结果发到这里。”，完成后发文字统计 + PNG 图。
- 月初第一场之前没有当月数据时，明确提示无当月对局；官方接口分页不完整时拒收，避免把半个月数据当整月。
- 图表使用 matplotlib Agg + 中文字体回退（Windows 微软雅黑 / Noto CJK / 文泉驿），无字体时退默认字体。
- 缓存：成功结果留一份旧数据，接口抖动时先给旧数据并注明缓存时间。

## 4. 验证

| 项 | 结果 |
|---|---|
| 本地单测（仓库 worktree，Python 3.12） | **10 项：9 通过、1 跳过**（跳过项是本机未装 matplotlib 的真实 PNG 渲染；服务端已装） |
| 服务端依赖 | `matplotlib 3.6.3`；`from bot.player_history import monthly_stats, monthly_chart_png` → OK |
| 线上文件与提交一致 | `commands.py` `89d3aa51`、`help.py` `6b5a4256`、`service.py` `bf7b1b02`、`player_history.py` `be5efe94`（git blob 前 8 位，四处均 SAME） |
| 部署与加载 | 四个文件 mtime `22:29:22`，`ygo-qq` 于 `22:29:36` 重启（MainPID 1080568、NRestarts=0）；`journalctl` 自 22:29 起 0 条 Traceback |
| 远端 | GitHub master `0632056`（`feat(qq): 新增“当月历史”指令…`，cherry-pick 自本地 `809a3b4`，作者保留） |

## 5. 回滚

- 代码：`git revert 0632056` 后重新部署四个文件；
- 线上：恢复备份（部署前的 `commands.py`/`help.py`/`service.py`/`player_history.py`），`python3 -m py_compile` 后重启 `ygo-qq`；
- **注意 QQ 观察期**：重启 `ygo-qq` 需用户同意并避开正在发送/收消息的窗口；本次 22:29:36 的重启是功能上线所需，之后没有再次重启。

## 6. 边界与后续

- 单测在本地跳过了真实 PNG 渲染（无 matplotlib），服务端已确认可导入并用 3.6.3；没有走真实 QQ 发消息验收（观察期不额外发消息），只做了代码/部署/日志核对。
- 当月口径按北京时间月初切；跨月第一场前后若官方接口回填延迟，可能出现短暂“当月无对局”，属接口侧延迟。
- 图表字体依赖系统字体；换机/容器化时要确认 `/usr/share/fonts/...` 仍存在，否则中文会变方框。
- 与 `历史` 共用同一套缓存/旧结果逻辑，两个命令的缓存键不同，互不覆盖。

## 7. 证据

- 提交：`0632056`（master）；原始本地提交 `809a3b4`。
- 单测命令：`PYTHONPATH=.;tests python -m unittest test_player_history -v`（仓库 worktree）。
- 线上核对：`sha256`/git blob 对比、`systemctl show ygo-qq`、`journalctl -u ygo-qq --since 22:29`。
