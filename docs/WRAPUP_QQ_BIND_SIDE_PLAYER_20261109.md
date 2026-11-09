# QQ 绑定 / 订阅空格 / 换备 / 玩家资料 + 最近5场 Side 收尾（2026-11-09）

> 状态：**已部署**。2026-11-09 11:31:44 重启 `ygo-web`、`ygo-qq`；GitHub `master` 提交 `bdb5687`（代码+测试）与 `0678c89`（文档）。
> 线上代码目录 `/opt/ygobot`；本文对应批次备份 `data/config-backups/qq-batch-20261109/`。

## 1. 背景与需求

本轮连续确认的功能：

1. QQ 账号绑定 MC 玩家名：`绑定mc`，之后 `历史`/`当月历史`/`构筑`/`录像` 不带名字默认查自己。
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

### 2.2 订阅空格名

- `订阅` 从右往左只识别末尾的 `全部`/`前N` 与 `推送`/`静默`，剩下全部算玩家名；至少给名字留一个词，兼容名字本身叫“推送/全部/前100”。
- 新增 `Store.player_names()`，把输入的空格写法对齐到最近 `stat_views`/`replays`/已有订阅里的真实名字（如 `chatLDS` → `chatLDS `、`小乔流水人家` → 带首尾空格的完整名、`lxyz 梅林` → `lxyz  梅林`）；唯一匹配才替换；清单缓存 5 分钟。
- 用于 `订阅`/`推送`/`静默`/`取消订阅`，以及显式玩家名的 `历史`/`当月历史`/`构筑`/`录像`/绑定。

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
| `bot/help.py` | `7f50450e` | 帮助同步 |
| `bot/qq.py` | `6951ceaa` | 合并转发支持图片节点 |
| `bot/statistics_side_recent.py` | `5e0cc48b` | 新增换备汇总 |
| `bot/statistics_portal.py` | `90948229` | `side-recent` 路由 |
| `bot/statistics_build.js` | `8a956d7b` | 最近5场 UI |
| `bot/statistics_build.html` | `cc288a32` | 最近5场控件/表格 |
| `bot/statistics_side_analysis.py` | `3dc1b41f` | 同步生产版（digest/scope） |
| `bot/statistics_build_detail.py` | `fdd769d4` | 同步生产版（空格/首尾修复） |

## 4. 部署记录

- 备份：`data/config-backups/qq-batch-20261109/baseline/`（8 个被覆盖文件 + `qq.py`；新增模块无基线）。
- 部署：`cp` staged 9 个文件 → `chown ygo:ygo` → `py_compile` → `systemctl restart ygo-web` → `systemctl restart ygo-qq`。
- 重启时间：2026-11-09 11:31:44；`ygo-web` MainPID 1215409、`ygo-qq` MainPID 1215412，均 active、`NRestarts=0`。
- 健康：`8787/health=200`、`8788/health=200`；`qq-status.json` = `online`、`process_id=1215412`、`expected_self_id=3528698561`；`bot.log` 最近 100 行 Traceback=0；`ygo-web` RSS 约 70 MB。
- 线上静态/接口：
  - `/statistics/build?player=祈祈巧` 200，含 `sideWindow`、`sideRecent`、`recentFirstIn`；
  - `/statistics/assets/statistics_build.js` 200，`sideWindow` 8 次、`sideRecent` 7 次；
  - `/statistics/api/side-recent?player=祈祈巧&top=6&recent=5` 200、30,452 字节、6 个对手、当前构筑 20 场。

## 5. 验证

- 本机 39 项相关单测通过：
  - `test_bind_mc` 12、`test_subscribe_spaces` 9、`test_side_plan_qq` 3、`test_player_profile` 6 = 30 项 QQ；
  - `test_statistics_side_recent` 9 项（聚合/分页/未知卡组/同场最大张数/先后攻）。
- 服务器 Python 3.12：staged `py_compile`、`server-smoke-bind.py`、`probe_service.py`（祈祈巧 → 2 页）、`probe_api.py`（200、6 对手、30KB）通过。
- GitHub：`master` 提交 `bdb5687`（代码+测试）；本文与文档为后续 docs 提交。
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
