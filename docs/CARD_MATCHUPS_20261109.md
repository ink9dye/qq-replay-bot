# 单卡胜率与“对前百卡组 Top5”（2026-11-09）

> 状态：**已上线**（网页 + QQ），线上验收通过。本文是这次改动的长期说明；一次性截图与浏览器证据在 `workflow-results/card-matchups-20261109/`。

## 1. 需求

用户要在卡片详情页看到：这张卡**对哪些前百卡组好用**，并给出各自胜率；随后明确 QQ 端也要输出“单卡胜率 + 对前百卡组最高的五个卡组及胜率”。

确认后的口径：

| 项 | 取值 |
|---|---|
| 我方 | 当前前 100 玩家（`stat_current_top100`） |
| 对手 | **也必须是当前前 100 玩家**；对手不在榜、对手卡组未知、对手不唯一的对局不计入本表 |
| 指标 | 主卡 `used=1` 的小局胜率；平局进分母 |
| 对手分组 | 对手构筑的 `deck_type`（精确卡组名） |
| 每行样本 | 至少 10 局；不足 5 个卡组时用 5–9 局补足并标“样本少” |
| 排序 | 胜率降序，同胜率局数多者优先，再按卡组名 |

“使用后”只认 `stat_card_facts.used=1`；额外卡在网页端按 `appeared=1` 显示为“出场后胜率”。QQ 命令固定主卡。

## 2. 统计实现

- `bot/card_statistics.py`
  - `matchups(db, selected, facts, partition, limit=5, min_games=10, fallback_games=5, opponents=None)`：只读聚合，返回 `base`（双方前100、使用后、胜负明确的总局数/胜率）、`rows`（对手卡组、胜/负/平、局数、胜率、Wilson 95% 区间、`low_sample`）、`notes`。
  - `query(..., matchups_enabled=False)`：只有显式请求才计算，避免影响既有调用。
  - 选中集先落连接内临时表 `_card_statistics_selected`，卡片事实与卡数只取选中构筑；`annotate_series` 仅在按系列筛选时开启，减少无关全表注解。
- `bot/card_identity.py`
  - `facts()` 在传入选中集时改为 `stat_card_facts f CROSS JOIN 选中集 s`：先从 `(code,partition)` 取该卡的少量事实，再回连选中集；避免先扫全卡事实再逐行回连（单卡查询从约 60s 降到约 20–46s，QQ 专用路径更快）。
- `bot/statistics_portal.py`
  - `/statistics/api/card-metrics` 新增 `matchups=1` 参数；只接受 `0/1`，结果随原有 600s 查询缓存。
- `bot/card_winrate.py`（QQ 专用）
  - 复用 `top100.eligible_views` 与 `card_statistics.matchups`；只保留双方前100的视角。
  - 30 秒“双方前100人口快照”（按名册版本、分类 revision、有效对局数/最新入库、排除数失效）+ 每张卡 30 秒结果缓存；最多 4 份人口快照、16 份卡结果。
  - 实测：灰流丽冷查询约 3.8s，同卡二次 0.17s；另一张卡（增殖的G）约 1.55s。

## 3. 展示

### 网页

`/statistics/cards?code=…` 在指标卡与投入分布之间新增面板：

- 标题随分区变化：“对前百卡组 · 使用后胜率 Top 5” / “出场后胜率”；
- 口径说明 + 总体（胜/负/平、局数）；
- 表格：对手卡组、胜率、胜/负/平、样本、95% 区间；“样本少”标记；
- 固定按双方当前前100重算，不受上方“我方/对手范围”下拉影响；卡组、先后攻、分区等筛选仍生效。

### QQ

新增命令：

```
单卡胜率 卡名
单卡胜率 14558127
```

输出顺序：总体使用后胜率 → 对前百卡组最高的五个 → 口径说明。示例（灰流丽，2026-11-09 14:24 数据）：

```
前100单卡胜率：灰流丽（14558127）
使用后胜率 52.2%（143胜/131负/0平，共274局；双方当前前100）
对前百卡组最高的5个（使用后）：
1. 白鸦艮神鬼 66.7%（8胜/4负/0平，12局）
...
口径：双方当前前100；主卡实际使用且胜负明确；平局计入分母；每行至少10局，不足时用5局以上补足并标注。
```

命令已加入慢命令队列（先回“单卡胜率统计中，约几秒…”），并写入 `help.py`。

## 4. 验证

- 单测：新增 `test_card_statistics` 2 项、`test_card_winrate` 2 项、`test_qq_new_commands` 1 项；连同相关套件共 8 项通过。
- 静态：`py_compile`、`node --check` 通过；页面 `$('id')` 与 `matchup*` ID 全部存在。
- 线上接口：`/statistics/api/card-metrics?code=14558127&partition=main&self_range=100&opponent_range=100&first=all&matchups=1&full=1` → 200；灰流丽总体 274 局 / 52.2%，5 行。
- 真实浏览器：headless Edge 桌面 1400×1800、移动 390×1600 均加载出面板、5 行、无 `window.error`/`unhandledrejection`。证据：[acceptance.json](../workflow-results/card-matchups-20261109/acceptance.json)、[移动端](../workflow-results/card-matchups-20261109/acceptance-mobile.json)、[桌面截图](../workflow-results/card-matchups-20261109/card-matchup-panel.png)。
- 服务：`ygo-web` / `ygo-qq` / `ygo-collector` active；8787/8788 health=200；QQ `online`、无 Traceback；web RSS 约 133MB、qq 约 30MB。
- 已知测试噪音：`test_card_statistics` 整套的既有 `missing_source_views` 用例会受 `top100._ALIVE_CACHE` 跨测试污染，单跑通过；`test_card_identity` 整套同样有 1 个顺序相关失败，单跑通过。均与本次改动无关。

## 5. 部署、回滚、版本

- 备份：`/opt/ygobot/data/config-backups/card-matchups-20261109/bot/`
- 部署方式：`scp` → `chown ygo:ygo` → 服务器 `py_compile` → 14:22:54 重启 `ygo-web`（1249947）、14:22:59 重启 `ygo-qq`（1249964），与另一会话的 14:22 名次快照部署同批；QQ 重启已获用户确认（观察期）。
- 版本（sha256 前16）：
  - 新：`card_statistics 2542e9ae`、`card_identity 156fab6d`、`card_winrate b0fd8278`、`statistics_portal 5cdd9ca8`、`service 27d52288`、`commands 6d794674`、`help 195a865f`、`statistics_card_metrics.html 5c7cfb6b`、`statistics_card_metrics.js 6dd91a97`
  - 基线：`card_statistics 7554146d`、`card_identity f88cfa85`、`portal 90948229`、`service 42321cd1`、`commands b8f5b1a4`、`help 2b1ac56c`、`html b105e860`、`js 46957a3a`
- 回滚：恢复备份目录中的 9 个文件（`card_statistics`、`card_identity`、`store`、`statistics_portal`、`commands`、`service`、`help`、`statistics_card_metrics.html/js`），删除 `bot/card_winrate.py`，`chown ygo:ygo`，`py_compile`，`systemctl restart ygo-web ygo-qq`。
- GitHub：`664a7af`（代码+测试）+ `799268d`（冻结清单授权记录），已推送 `master`。

## 6. 影响面与边界

- 只新增只读接口字段、网页面板和 QQ 命令；既有卡片统计口径、排序、命名、取源不变；未改 NapCat/QQ 链路。
- 网页冷查询仍受 `CARD_METRICS_CACHE_SECONDS=600` 影响，首次约 20–46s；已有缓存后很快。静态 JS 有 1 小时缓存，验收需 `Ctrl+F5`。
- 对手卡组按精确 `deck_type` 分组；同名不同构筑变体会分开统计，因此前五可能被同一主大类的多个变体占据。若要改按“主大类”合并，需要另立口径，不在本次范围。
- 样本不足时只显示能达标的行，或标“样本少”；不会用 0 局/1 局伪造 100%。
- `matchup` 只负责“使用后”胜率，不表示因果提升；页面仍保留原有“抽到后”等指标。
