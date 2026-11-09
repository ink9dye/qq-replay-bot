# QQ 绑定 / 订阅空格 / 换备 / 玩家资料 + 最近5场 Side 收尾（2026-11-09）

> 状态：**已部署**。2026-11-09 11:31:44 重启 `ygo-web`、`ygo-qq`；GitHub `master` 提交 `bdb5687`（代码+测试）、`0678c89`（文档）、`0ae8572`（补记提交号）、`b2f288c`（帮助文案修正）、`fd08c2c`（最终收尾）。
> 线上代码目录 `/opt/ygobot`；本文对应批次备份 `data/config-backups/qq-batch-20261109/`。

## 1. 背景与需求

本轮连续确认的功能：

1. QQ 账号绑定 MC 玩家名：`绑定mc`，之后 `历史`/`当月历史`/`构筑`/`录像`/`换备`/`玩家` 不带名字默认查自己。
2. 订阅支持名字含空格：`订阅 久 远`、`订阅 久 远 前100 推送` 可直接写。
3. 新 QQ 指令 `换备 [玩家名]`：当前构筑对最常遇到的 6 个对手卡组，各取最近 5 场，按先攻/后攻分列换入/换出。
4. 网页构筑页 Side 面板默认改成“最近5场”，保留“全部历史”切换；矩阵 Side 不变。
5. 新 QQ 指令 `玩家 [玩家名]`：把历史、当月历史、构筑、换备、录像合并成一条聊天记录（合并转发）。

## 2. 实现要点

### 2.1 绑定与默认自己

- `bot.sqlite3` 新表 `qq_bindings(user_id PRIMARY KEY, player, updated_at)`；QQ 启动时 `CREATE TABLE IF NOT EXISTS`。
- 两步绑定：`绑定mc` 后在 120 秒内发名字；也支持 `绑定mc 玩家名`。群内等待期允许免 @；发已知指令会作废等待。
- 绑定成功用 `stat_views` 做一次“最近14天是否收录”的软提示，不阻塞绑定。
- `我的绑定` / `解绑mc`；未绑定而省略玩家名时提示先绑定。
- 默认自己覆盖：`历史`、`当月历史`、`构筑`、`录像`、`换备`、`玩家`。
- 帮助文案修正（12:34）：`绑定mc` 行只保留“绑定你的 MC 玩家名”；原文“下面四条不带名字就默认查自己”不准确（实际六条，且每行已单独说明）。

### 2.2 订阅空格名

- `订阅` 从右往左只识别末尾的 `全部`/`前N` 与 `推送`/`静默`，剩下全部算玩家名；至少给名字留一个词，兼容名字本身叫“推送/全部/前100”。
- 新增 `Store.player_names()`，把输入的空格写法对齐到最近 `stat_views`/`replays`/已有订阅里的真实名字（如 `chatLDS` → `chatLDS `、`小乔流水人家` → 带首尾空格的完整名、`lxyz 梅林` → `lxyz  梅林`）；唯一匹配才替换；清单缓存 5 分钟。
- 用于 `订阅`/`推送`/`静默`/`取消订阅`，以及显式玩家名的 `历史`/`当月历史`/`构筑`/`录像`/`换备`/`玩家`/绑定。

### 2.3 换备（QQ）与最近5场 Side（网页）

- 新模块 `bot/statistics_side_recent.py`：复用 `statistics_build_detail.query` 的 `match_groups` 和逐局 `changes`，只读汇总，不改 `infer_side`、排序、命名、取源。
- 口径：当前构筑（与 `构筑` 同一份最新构筑）遇到过的对手卡组按场次取前 6；每个对手取最近 5 场；用这 5 场里所有 `changes.status=known` 的第 2/3 局；同一场多局同一张卡取最大张数；先攻/后攻按该玩家首局实际座位；完整换边局少于 3 个标“样本少”；未知对手卡组不列入前 6。
- QQ 输出：文字，每方向最多列前 10 张，6 个卡组通常分 2 条消息；`TextPages` 负责分页发送。
- 新接口：`GET /statistics/api/side-recent?player=&digest=&top=6&recent=5`，30 秒缓存，只读。
- 网页：`statistics_build.html/js` 新增“统计范围：最近5场/全部历史”；最近5场只列前 6 个对手卡组，四张表（先/后攻 × 换入/换出）+ 逐场证据；全部历史仍走原 `side-analysis`。

### 2.4 玩家资料（QQ）

- `service._player_profile_job` 依次调用现有 `历史`/`当月历史`/`构筑`/`换备`/`录像`，每段加 `【标题】` 后放入合并转发节点；历史/当月/构筑图放在节点 `image` 段。
- `qq.py._send_forward` 支持 `{'text','images'}` 节点；图片节点失败自动重试纯文字合并转发；整体失败再逐段发送。
- 群聊可用；不写名字默认走绑定；未绑定先提示 `绑定mc`。

## 3. 线上版本（sha256 前 16）

| 文件 | 版本 | 说明 |
|---|---|---|
| `bot/commands.py` | `b8f5b1a4` | 绑定/订阅空格/换备/玩家解析 |
| `bot/store.py` | `db12af67` | `qq_bindings`、`player_names` |
| `bot/service.py` | `42321cd1` | 默认自己、换备、玩家资料 job |
| `bot/help.py` | `2b1ac56c` | 帮助同步（12:34 修正“下面四条”文案） |
| `bot/qq.py` | `6951ceaa` | 合并转发支持图片节点 |
| `bot/statistics_side_recent.py` | `5e0cc48b` | 新增换备汇总 |
| `bot/statistics_portal.py` | `90948229` | `side-recent` 路由 |
| `bot/statistics_build.js` | `8a956d7b` | 最近5场 UI |
| `bot/statistics_build.html` | `cc288a32` | 最近5场控件/表格 |
| `bot/statistics_side_analysis.py` | `3dc1b41f` | 同步生产版（digest/scope） |
| `bot/statistics_build_detail.py` | `fdd769d4` | 同步生产版（空格/首尾修复） |

## 4. 部署记录

- 备份：`data/config-backups/qq-batch-20261109/baseline/`（8 个被覆盖文件，含 `qq.py`；新增模块无基线）。
- 部署：`cp` staged 9 个文件 → `chown ygo:ygo` → `py_compile` → `systemctl restart ygo-web` → `systemctl restart ygo-qq`。
- 重启时间：2026-11-09 11:31:44；`ygo-web` MainPID 1215409、`ygo-qq` MainPID 1215412，均 active、`NRestarts=0`。
- 12:34:51 帮助文案修正（`绑定mc` 行去掉“下面四条”）：仅重启 `ygo-qq`，MainPID 1222756，active、无 Traceback；`help.py 2b1ac56c`。
- 健康：`8787/health=200`、`8788/health=200`；`qq-status.json` = `online`、`process_id=1222756`（12:34 帮助修正后的 ygo-qq）、`expected_self_id=3528698561`；`bot.log` 最近 200 行 Traceback=0；`ygo-web` RSS 70→139–145 MB，`memory.events` low/high/max/oom 全 0。
- 线上静态/接口：
  - `/statistics/build?player=祈祈巧` 200，含 `sideWindow`、`sideRecent`、`recentFirstIn`；
  - `/statistics/assets/statistics_build.js` 200，`sideWindow` 8 次、`sideRecent` 7 次；
  - `/statistics/api/side-recent?player=祈祈巧&top=6&recent=5` 200、30,452 字节、6 个对手、当前构筑 20 场。

## 5. 验证

- 本机 39 项相关单测通过：
  - `test_bind_mc` 12、`test_subscribe_spaces` 9、`test_side_plan_qq` 3、`test_player_profile` 6 = 30 项 QQ；
  - `test_statistics_side_recent` 9 项（聚合/分页/未知卡组/同场最大张数/先后攻）。
- 服务器 Python 3.12：staged `py_compile`、`server-smoke-bind.py`、`probe_service.py`（祈祈巧 → 2 页）、`probe_api.py`（200、6 对手、30KB）通过。
- GitHub：`master` 提交 `bdb5687`（代码+测试）、`0678c89`（文档）、`0ae8572`（补记提交号）、`b2f288c`（帮助文案修正）。
- 未发 QQ 测试消息（遵守观察期纪律）；合并转发图片节点由 mock 覆盖，NapCat 实发未主动触发。

## 6. 回滚

整批回滚：

```bash
cp -a /opt/ygobot/data/config-backups/qq-batch-20261109/baseline/commands.py \
      /opt/ygobot/data/config-backups/qq-batch-20261109/baseline/store.py \
      /opt/ygobot/data/config-backups/qq-batch-20261109/baseline/service.py \
      /opt/ygobot/data/config-backups/qq-batch-20261109/baseline/help.py \
      /opt/ygobot/data/config-backups/qq-batch-20261109/baseline/qq.py \
      /opt/ygobot/data/config-backups/qq-batch-20261109/baseline/statistics_portal.py \
      /opt/ygobot/data/config-backups/qq-batch-20261109/baseline/statistics_build.js \
      /opt/ygobot/data/config-backups/qq-batch-20261109/baseline/statistics_build.html \
      /opt/ygobot/bot/
rm -f /opt/ygobot/bot/statistics_side_recent.py
chown ygo:ygo /opt/ygobot/bot/commands.py /opt/ygobot/bot/store.py /opt/ygobot/bot/service.py \
  /opt/ygobot/bot/help.py /opt/ygobot/bot/qq.py /opt/ygobot/bot/statistics_portal.py \
  /opt/ygobot/bot/statistics_build.js /opt/ygobot/bot/statistics_build.html
systemctl restart ygo-web ygo-qq
```

`qq_bindings` 表保留不删；回滚代码不会使用它。

## 7. 已知边界

- “当前构筑”沿用 `构筑` 的最新构筑（主卡相同）。若最新构筑只有 1 场，`换备` 只显示 1 个对手并标“样本少”；用户已确认保持该口径，不改“最近14天场次最多的主构筑”。
- `玩家` 依次读取 5 项，历史/当月历史受官方接口耗时影响，整条约 30 秒；合并转发失败会逐段发送。
- QQ 每方向最多列前 10 张，完整明细看网页。
- 网页“最近5场”默认只统计当前构筑；切“全部历史”才回到原聚合口径，矩阵 Side 不变。
- 静态资源有 1 小时缓存；浏览器验收前 Ctrl+F5。

## 8. 2026-11-09 修正：换备先/后攻改按“该换边局座位”（已上线）

- 现象：用户看 `换备 亚翁` 第 1 组（x剑削手米底乌斯…，6 场/最近 5 场/9 个 Side 局），“先攻”栏出现 `+原始生命态 尼比鲁×3`、`-小丑与锁鸟` 这类后攻备牌；两方向进出张数也对不上（先攻 12进/13出、后攻 12进/14出）。
- 真因：`build_plan` 用**首局** `first_player` 给整场贴先/后攻标签，但 `changes` 是第 2/3 局对第 1 局的差量，生效在**第 2/3 局**；BO3 里第 2/3 局座位常与首局相反（亚翁这 5 场第 2 局全部相反），于是大部分备牌被贴反。
- 修正：座位取该换边局自己的 `first_player`；基准仍是主卡局（第 1 局）主卡+额外，第 2/3 局各自和主卡局比，不链式；同场多局同卡取最大张数不变；座位缺失的局不计入任何方向，单独计数 `seat_unknown_games` 并在 notes 提示。
- 代码：`statistics_side_recent.py`（分桶，新增 `side_seats`/`seat_unknown_games`）、`statistics_build.js`（证据栏显示换边局座位，表头“换边局先后攻”）、`statistics_build.html`（说明文案）。
- 版本（sha256 前 16）：`statistics_side_recent 6b277e7c`、`statistics_build.js bddbcdd4`、`statistics_build.html fefe6b1d`；基线 `5e0cc48b` / `8a956d7b`（CRLF，LF 归一 `fbc236c4`）/ `cc288a32`。
- 部署：2026-11-09 13:52:51 重启 `ygo-web`（MainPID 1238402）、`ygo-qq`（1238415）；8787/8788 health=200、QQ `state=online`、`bot.log` 最近 200 行 Traceback=0；备份 `data/config-backups/side-seat-20261109/`（含 before/after sha256）。
- 验证：本机 12 项单测通过（新增 3 项：第 2 局座位与首局相反、一场两局分别进两个方向、座位缺失不猜）；服务器影子副本跑亚翁/祈祈巧真实数据；上线后 `side-recent?player=亚翁` 先攻 4 局/后攻 5 局、`side_seats` 正确、页面与静态 JS 均含新文案。
- 回滚：`cp -a data/config-backups/side-seat-20261109/statistics_side_recent.py data/config-backups/side-seat-20261109/statistics_build.js data/config-backups/side-seat-20261109/statistics_build.html bot/` → `chown ygo:ygo` → `py_compile` → `systemctl restart ygo-web ygo-qq`。
- GitHub：`339a600`（`fix(side): 换备先/后攻按该换边局座位分列`，代码+测试+本文件 §8+DECK_BUILD_SIDE §2.6），已推送 `master`。
- 边界：第 3 局同样“对主卡局”取净差，若第 2 局换入的牌第 3 局保留，它会同时出现在两个方向（亚翁的 `尼比鲁×1` 即此），属“基于主卡局”口径的必然结果，不是重复计数。
- 未改项：「全部历史」（`side-analysis`，含投入率/算法候选）与矩阵 Side 仍按**场次首局座位**筛先后攻，本次只统一「最近5场 / QQ 换备」；两处对同一场比赛的方向归属可能不同，要统一需另开改动（会触 Side 筛选冻结口径）。

## 9. 2026-11-09 14:03 换备只看对手前 200（已上线）

- 用户要求：`换备` 里列出的卡组应是“它打前 200 的卡组”，对局数量也只算前 200 的对手。
- 实现：`statistics_side_recent.query` 取完 `build_detail`（当前构筑仍是该玩家最新构筑，不因过滤而退化）后，用 `_rank_map`（`stat_current_top100` + `statistics_rank_scope.stored_ranks` 的两份名次快照）给每场对手排名，`_scope_groups` 只保留名次已知且 ≤200 的场次；名次缺失的对手不猜、直接不计入，计数写进 `rank_scope`（`excluded_matches` / `unverified_matches` / `matches_before_scope`）。
- 展示：QQ 首行口径加“对手限前200”；网页 `sideRecentMeta` 显示“对手限前200”，面板说明与 notes 同步；新增 `stored_ranks(db, now)` 公共只读封装（`statistics_rank_scope.py`，+5 行）。
- 示例（亚翁，当前构筑）：50 场 → 23 场（26 场对手名次在前 200 之外、1 场未核验），前 6 卡组从“x剑削手…/三幻魔/盈彩月夜/烙印星辰/雷盟/死狱乡烙印”变为“x剑削手…(3)/三幻魔(3)/雷盟(3)/死狱乡烙印(2)/幕帘味美喵(2)/米底乌斯…(2)”；第 1 组 3 场 5 个 Side 局，先攻 2 局 / 后攻 3 局。
- 验证：本机 16 项 `test_statistics_side_recent` 通过（新增 4 项：只留已知前200、无名次不计入、notes/rank_scope、文本含“对手限前200”）；服务器 `/tmp` 影子副本跑亚翁；上线后接口 `side-recent?player=亚翁` 返回 `rank_scope`、页面含“只统计对手在当前前200名内”、静态 JS 含“对手限前/rank_scope”。
- 版本（sha256 前 16）：`statistics_side_recent 14e18590`、`statistics_rank_scope 6a7163ea`、`statistics_build.js ccab448a`、`statistics_build.html 1f36f381`；基线 `6b277e7c` / `6c88ad07` / `bddbcdd4`（LF 归一 `1aa2989b`）/ `fefe6b1d`。
- 部署：2026-11-09 14:03:02 重启 `ygo-web`（MainPID 1243256）、`ygo-qq`（1243270）；8787/8788 health=200、QQ `online`、日志无 Traceback；备份 `data/config-backups/side-top200-20261109/`（4 文件 + before/after sha256）。
- 回滚：`cp -a data/config-backups/side-top200-20261109/{statistics_side_recent.py,statistics_rank_scope.py,statistics_build.js,statistics_build.html} bot/` → `chown ygo:ygo` → `py_compile` → `systemctl restart ygo-web ygo-qq`。
- 已知差异：`ygo-web` 开 quality filter、`ygo-qq` 不开，同一玩家网页显示 41→20、QQ 显示 50→23；两处都只算前200，但“共 N 场”的分母不同，属既有差异（本次未改 quality 开关）。
- GitHub：`50bf472`（`feat(side): 换备只看对手前200`，代码+测试+本文件 §9+DECK_BUILD_SIDE/FEATURES），已推送 `master`。
- 14:10 调整（同日上线）：名次快照新鲜度 7 天 → **14 天**（用户要求与对局窗口一致）。`statistics_rank_cache.load(data_dir, now, ttl=)` 与 `statistics_rank_scope._stored_ranks/stored_ranks(max_age=)` 参数化，默认仍 7 天，只有换备传 14 天；`rank_scope.rank_snapshot_days=14`、notes 写“14天内名次快照”。当前索引为 11-08 19:08（约 1.7 天），所以线上数字当天不变，只在快照 7–14 天时兜住。版本：`statistics_rank_cache e64e9f4f`、`statistics_rank_scope 2cc7f1fc`、`statistics_side_recent 6117278e`；14:10:24 重启 `ygo-web`（1247439）/`ygo-qq`（1247453）。GitHub：`5fc729c`，已推送 `master`。
- 14:22 再调（同日上线）：用户要求全站统一，名次快照默认值本身也改成 **14 天**——`statistics_rank_cache.TTL = 14 天`、`_stored_ranks`/`stored_ranks` 的 `max_age` 默认 14 天（矩阵/前200 等入口一并生效）。版本：`statistics_rank_cache 22937345`、`statistics_rank_scope a8d183de`；14:22:54 重启 `ygo-web`（1249947）/`ygo-qq`（1249964）。复核：`side-recent` 的 `rank_snapshot_days=14`、矩阵 200/200 仍 6/6 场且 `rank_scope.complete=true`。GitHub：`5dfa993`，已推送 `master`。

