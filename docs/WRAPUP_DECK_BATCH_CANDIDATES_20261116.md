# 录像筛选「卡组候选」确定报错与下拉空白修复收尾（2026-11-16）

> 结论：点位不是“慢”，是两处独立缺陷。① 前端在检查名单之前就用“名次未核验”整批拒收，200/200 下几乎必然触发；② 候选接口必须等后台队列空闲才算，缓存过期时只回 503，输入框一分钟以上没有名字。已只改两个前端文件并上线，未重启任何服务。

## 0. TL;DR

| 项 | 结果 |
|---|---|
| 现象 | “我方/对手卡组”输入后点确定报“部分玩家排名尚未核验，请稍后重试；未加入不完整名单”；对手下拉始终没有卡组名 |
| 根因 A | `statistics_autocomplete.js` 的 `if (!result.complete) throw` 在取 `names` 之前执行，未核验=整批丢弃；而 200/200 下 `complete=false` 是常态 |
| 根因 B | 候选走 `QueryRefresh(background=True)`：仅缓存命中即时返回，否则 503 后台算，且需队列空闲才排得进；实测 `deck-options` 74.6s / 34.3s |
| 附带 bug | `statistics_recordings.html` 的 `loadDeckChoices` 把 `groups.deck` 同时填进对手 datalist |
| 修复 | 未核验降级为提示（不计入对局，不再拒收）+ 满 100 上限按名额加入并说明剩余 + 双方各用各的名字 + 按范围缓存 + 页面加载后预取 |
| 版本 | master 已提交并 push，远端一致 |
| 部署 | 只替换两个静态文件（11:02），**未重启**；`ygo-web` PID 2690952 不变，8787/8788 健康 200 |
| 回滚 | 备份 `/opt/ygobot/data/config-backups/deck-batch-candidates-20261116-1105/`；覆盖回两文件 + Ctrl+F5 即可，无需重启 |

## 1. 证据

线上候选缓存 `data/query-cache/last-success/labels-*.json`（`saved` 为生成时刻）：

| 生成时刻 | 范围 我方/对手 | complete | pending | 我方卡组 | 对手卡组 |
|---|---|---|---|---|---|
| 11-16 10:52:56 | 200/200 | **false** | 0 | **142** | **230** |
| 11-16 10:57:19 | 100/all | true | 0 | 101 | 538 |
| 11-15 00:57:48 | 200/all | false | 1 | 175 | 995 |

10:52:56 那条就是用户点确定时的记录：名字已经算出来了（142/230），`pending=0` 说明不是“正在核验”，而是这批人已失败进冷却。前端拿 `complete=false` 把 142 个名字全丢了。

`bot/statistics_rank_scope.py` 的核验能力决定了 200/200 很难 `complete`：

- 第 122 行 `wait(futures,timeout=1)`：每轮只等 1 秒；
- `_POOL=ThreadPoolExecutor(max_workers=4)`，同时在飞上限 `len(_PENDING)<32`；
- 失败进入 60/120 秒冷却。

前 200 会带进上千名非前 100 玩家，一轮根本查不完；冷却期重试也拿不到结果，所以“稍后重试”对 200/200 基本无效。

同期 `data/logs/statistics-query-timings.jsonl`（最近几条，秒）：

| kind | elapsed | queue_wait |
|---|---|---|
| card-report | 142.0 / 65.0 / 73.7 / 72.5 | 0 / 71.8 / 169.1 / 0 |
| deck-options | 74.6 / 34.3 | 0 / 0 |
| group-list | 36.6 / 48.7 / 19.5 / 39.8 | 0 |

候选任务要和这些长任务抢同一个“队列空闲”闸门，所以下拉经常一分钟以上拿不到名字；前端 `api()` 对 `query_busy` 最多重试 10 分钟，超时后把 503 原文抛给用户。

## 2. 改动（仅前端两文件）

| 文件 | 改动 | 作用 |
|---|---|---|
| `bot/statistics_autocomplete.js` | 删除 `if (!result.complete) throw`，改为 `partial` 提示文案；加入逻辑改为“按剩余名额取 `fresh`，满 100 时说明另有 N 个未加入” | 未核验不再丢整批候选；142 个匹配名也不会再撞“最多100个”直接报错 |
| `bot/statistics_recordings.html` | 对手 datalist 改用 `groups[id]`（对手=`groups.opponent`）；候选缓存键改为 `self_range/opponent_range` 串；范围变更自动重取；`load()` 之后 `setTimeout(...,2000)` 预取；失败提示改为“还在后台准备” | 对手有名字；换范围不再吃旧名单；进输入框时名字已在；不再抢占主查询队列 |
| 同上 | 脚本地址 `?v=20261116batch` | 避免浏览器用旧脚本 |

口径未变：未核验的玩家依旧不计入统计结果，只是不再阻断“加入候选”。

## 3. 验证

- `node --check statistics_autocomplete.js` 通过；页面内联脚本单独抽出 `node --check` 通过。
- 临时 DOM 桩跑真实代码路径：喂 142 个名字 + `complete=false` → 实际加入 100 个，提示含“另有 42 个匹配卡组未加入”与“部分玩家排名未核验”，**不再抛错**。
- 线上文件 rawSHA 前 16：`61bd89c0aa0d58aa`（js）/ `86545271b49e0a57`（html），与本地逐字节一致；替换前原文件为 `ffe1dd0ab2b974a4` / `ccf48f0c8e799885`，与备份一致。
- `/statistics/recordings` 返回 `Cache-Control: private, no-store`，页面已经是 `?v=20261116batch`；`/statistics/assets/statistics_autocomplete.js?v=20261116batch` 200 且不含旧拦截串。
- 服务：`ygo-web` PID 仍 2690952（未重启），`ygo-web`/`ygo-qq`/`ygo-collector` active，8787/8788 健康 200。

## 4. 使用与回滚

- 使用：Ctrl+F5 一次；范围设 200/200 后，首次（或隔十分钟）候选仍需后台算 30–75 秒，期间提示“还在后台准备”，算完名字自动出现；确定按钮最多提示“另有 N 个未加入”。
- 回滚：`cp` 备份目录两文件回 `/opt/ygobot/bot/`，保持 `ygo:ygo 644`，Ctrl+F5；纯静态，不需要重启服务。

## 5. 未做（留给后续）

- 100 个卡组上限未调整（如需 150/200 要同时改服务端校验与前端提示）。
- 让 labels 候选任务不吃“必须队列空闲”限制需改后端并重启 web，未做。
- 开发树仍混有他人未上线改动（`docs/INDEX.md` 两行 topic 链接指向未跟踪文件）。本次只提交这两个文件、本文与索引行；索引按“HEAD + 本文一行”单独写入索引，不带他人未完成的链接。
