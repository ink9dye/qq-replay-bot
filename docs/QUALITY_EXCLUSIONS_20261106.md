# 统计口径永久过滤：胜负未知整场与局次未知小局（2026-11-06）

目标：已入库录像中**胜负不确定的整场**、**第几局不确定的小局**以及它们的同场关联局，不再进入统计。最终实现为查询层过滤，不再依赖一次性排除表；`stat_exclusions` 中本轮自动行已清零。

## 1. 口径

1. **整场排除**：同一场（可靠 `session_id` 一致）内只要有任意一局胜负未知，该场所有已入库小局一起排除。不能只删未知那一局而保留第 1、2 局，否则胜率、Side、构筑历史仍会引用不完整场次。
2. **单局排除**：没有可靠正整数局次的小局单独排除；有局次但无法关联到可靠场次、且胜负未知的小局也单独排除。
3. **不猜**：可靠来源仅限 capture、上传账本 `capture`、已确认身份、唯一 `replay_sessions` 关联。不用文件名、上传顺序或时间邻近补局次。

## 2. 实现

- 新模块 `bot/statistics_quality.py`：
  - `compute(db)` 读取 `stat_views` 的胜负、`statistics_match_identity.identity_metadata` 的场次/局次，生成排除摘要集合；
  - `excluded_digests(db)` 在进程内缓存结果，用 `stat_views`/`stat_games`/`stat_evidence`/`stat_files`/`replay_sessions` 的计数、最大 rowid、已知胜负计数，以及上传账本、身份确认库的 mtime 作为失效指纹；数据变化后自动重算，无需重启；
  - 计算异常时 **fail-open**：返回空集合且不缓存，避免一次锁/证据读取失败把所有统计清空。
- `bot/top100.py` 的 `eligible_views()` 在 SQL 取数后、文件存活检查和首局继承之前过滤；录像筛选、卡片统计、额外卡、矩阵、玩家页、构筑详情、Side 分析都走这个入口。
- `bot/statistics_server.py` 的 `create_server()` 从配置 `statistics.quality_filter.enabled` 启用，仅 `ygo-web` 打开；QQ/collector 不启用，行为保持不变。
- 性能：首次计算约 1.0s，命中缓存约 0.02s；热查询约 0.17s。回填活跃时指纹会变化，下一次查询重新计算约 1s。

## 3. 数据与验证（2026-11-06 23:15）

| 项 | 数值 |
|---|---|
| `stat_views` / 摘要 | 29,668 / 14,834 |
| 排除摘要 | **2,418**（1,029 整场 + 1,388 局次未知 + 1 单局未知） |
| `stat_exclusions` 自动行 | **0**（一次性补跑后已清零） |
| 录像筛选 total | 5,266 |
| 已确认胜负分母 | 5,266 |
| 胜负未知 | **0** |

- `outcome=unknown` 筛选返回 0。
- 直接检查过滤后的 5,266 个有效视角：胜负未知 **0**、无可靠局次 **0**。
- 固定三局样本（第 1/2 局已知、第 3 局 unknown）整场不再出现；正常已知对局仍在。
- 冷查询约 7s（含查询排队与重试），热查询约 0.10s；过滤计算约 1.0s，缓存命中约 0.02s。
- 一次性补跑历史：20:39 写入 2,259 行；22:58 补跑到 2,418 行；查询层过滤验证通过后删除全部 2,418 行，避免与查询层重复并恢复分享/打包的原始可达性。删除前快照为 `stat_exclusions-before-filter-20261106-2308.tsv`。

## 4. 部署与回滚

- 文件：新增 `bot/statistics_quality.py`；修改 `bot/top100.py`、`bot/statistics_server.py`。
- 提交：实现提交 `8110736`（已推送）；本文件后续数字修订随最新提交同步。
- 配置：`statistics.quality_filter = {"enabled": true}`；备份在 `/opt/ygobot/data/config-backups/quality-filter-20261106/`（含旧 `top100.py`、`statistics_server.py`、`config.json`）。
- 只重启 `ygo-web`；`ygo-collector`、`ygo-qq`、`ygo-replay-api`、nginx 未因本轮改动重启。
- 回滚：
  1. 把 `statistics.quality_filter.enabled` 改为 `false`（或删除该键），重启 `ygo-web`，立即恢复旧口径；
  2. 需要连代码一起回滚时，用备份目录覆盖两个旧文件并删除 `bot/statistics_quality.py`，再重启 `ygo-web`；
  3. 如要恢复一次性排除快照，用运维工作区的 `quality_exclusions.py` 对 `manifest-2258.tsv` 重放。
- 无数据库迁移；`stat_exclusions` 仅保留人工排除（当前 0 行）。

## 5. 边界

- 只作用于 `ygo-web` 的统计查询；设备页“最近入库”、QQ 状态列表、`/replay-api` 录像库仍按原始记录展示。
- QQ 的统计命令（前百饼图/构筑等）不走该过滤；如需同样口径，要在 QQ 进程启用同一模块并重启。
- 手动排除表 `stat_exclusions` 仍然独立生效；查询层过滤不写该表。
- 回填/晚到数据会自动纳入判断：新写入的未知局在下一次指纹变化后排除；晚到胜负把 unknown 变成已知后会重新进入统计。
- 冷查询会增加约 1s 计算；回填频繁时可能重复计算。

## 6. 测试

- `tests/test_statistics_quality.py`：5 项通过（整场关联、单局未知、无局次、缓存失效、fail-open、`eligible_views` 集成）。
- 相关回归：`test_top100` 11 项、`test_statistics_recording_filters` 6 项、`test_card_statistics` 9 项、`test_statistics_server` 1 项通过；`test_player_statistics`/`test_statistics_detail` 各有 1 项既有环境失败（与本轮改动无关，过滤默认关闭时同样失败）。
