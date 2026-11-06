# 统计口径排除：胜负未知整场与局次未知小局（2026-11-06）

本轮把已入库录像中**胜负不确定的整场**和**第几局不确定的小局**从统计口径排除。只写生产库 `stat_exclusions`，不删录像、不删 `stat_views`/`stat_games`、不改代码、不重启服务；回滚只需删除本轮写入的 `auto:%` 行。

## 1. 判定口径

1. **整场排除**：同一场（可靠 `session_id` 一致）内只要有任意一局胜负未知，该场所有已入库小局一起排除。不能只删未知那一局而保留第 1、2 局，否则胜率、Side、构筑历史仍会引用不完整场次。
2. **单局排除**：没有可靠正整数局次的小局单独排除；有局次但无法关联到可靠场次、且胜负未知的小局也单独排除。
3. **不猜**：可靠来源仅限 capture、上传账本 `capture`、已确认身份、唯一 `replay_sessions` 关联。不用文件名、上传顺序或时间邻近补局次。

## 2. 数据规模（执行时）

| 项 | 数值 |
|---|---|
| 玩家视角 / 录像摘要 | 28,268 / 14,134 |
| 胜负未知摘要 | 2,033（双方视角均为 unknown） |
| 有可靠身份摘要 | 12,905 |
| 无可靠局次摘要 | 1,229 |
| 至少一局未知的场次 | 400 |
| 这些场次涉及的摘要 | 1,029（其中 84 份本身胜负已知） |
| 无场次但局次已知、胜负未知 | 1 份（`a6adedf5…`，局次 1） |
| 合计排除 | **2,259 份摘要** |

清单构成：`auto:match_unknown_outcome` 1,030 行（946 份未知 + 84 份关联已知）、`auto:game_number_unknown` 1,229 行（1,087 份未知 + 142 份胜负已知但局次未知）。涉及 666 名玩家；400 场中 23 场 1 局、125 场 2 局、252 场 3 局。

首轮按“整场 + 局次未知”写入 2,258 行；复核发现 `a6adedf5…` 有局次 1、无场次、胜负未知，未被两条规则覆盖，补入后为 2,259 行。

## 3. 写入方式

- 表：`stat_exclusions`；行：`player=''`（对双方生效）、`scope='default'`、`reason='auto:match_unknown_outcome'` 或 `'auto:game_number_unknown'`。
- 以 `ygo` 用户单事务 `BEGIN IMMEDIATE`：先删除旧 `auto:%` 行，再 `INSERT OR IGNORE`，一次提交；可重复运行，按当时数据重算。
- 执行前该表 0 行；执行后仅本轮 2,259 行，无其它行。DB/WAL/SHM 属主仍为 `ygo:ygo`，WAL 仍 128MB。

## 4. 生效验证

录像筛选接口（8788，owner cookie）：

| 指标 | 执行前 | 执行后 20:43 | 复查 20:49 |
|---|---|---|---|
| total | 6,316 | 4,922 | 4,876 |
| 已确认胜负分母 | 5,080 | 4,922 | 4,876 |
| 胜负未知 | 1,236 | 0 | 0 |
| 胜 / 负 | 3,017 / 2,063 | 2,916 / 2,006 | 2,887 / 1,989 |

- 4,922 与直接调用 `eligible_views` 一致（源文件存活检查后）；中间值 9,396→7,873（`excluded_current_views=1,523`）。
- 20:49 的 4,876 是 14 天保留期清理了 64 个视角（32 局，其中 18 个属已排除视角），不是排除规则变化。
- 固定三局样本（第 1/2 局已知、第 3 局 unknown）的 3 份摘要均不再出现在 `eligible_views`；正常对照仍在。
- `outcome=unknown` 筛选返回 0；`ygo-web`/`ygo-collector`/`ygo-qq`/`ygo-replay-api` 均 active，8787/8788 `/health` 200，journal 无新增 Traceback。

## 5. 影响与边界

- **只影响统计口径**：录像文件、`stat_views`/`stat_games`/`stat_builds` 均未删除；录像库（`/replay-api`）仍列出这些文件。
- 分享构筑深链对已排除摘要返回 404“来源已失效或被排除”；录像页打包下载直接指定已排除摘要会被拒。
- 排除行 `player=''` 不出现在 owner 复核页的“按玩家排除”列表，不能从网页逐条恢复；回滚用 SQL。
- **一次性回填**：只覆盖执行时已入库的视角。之后新入库的未知局不会自动排除；需要时重跑脚本。
- 设备页“最近入库”和 QQ 状态列表不读该排除表，仍显示原始入库记录。

## 6. 回滚与重跑

回滚（只删本轮自动行，≤30 秒缓存后恢复）：

```sql
DELETE FROM stat_exclusions
 WHERE player = '' AND scope = 'default' AND reason LIKE 'auto:%';
```

重跑：脚本在运维工作区 `workflow-results/quality-exclusions-20261106/quality_exclusions.py`（默认 dry-run，`--apply` 写入，`--manifest` 输出清单）；未安装到 `/opt/ygobot/tools/`。以 `ygo` 用户执行，并先确认当时没有正在进行的统计刷新。

## 7. 证据

运维工作区 `workflow-results/quality-exclusions-20261106/`：

- `README.md`：完整处理说明；
- `manifest.tsv`：2,259 行清单（含玩家名与摘要，不随本文档提交仓库）；
- `before.tsv`：执行前快照（表头）；
- `apply.json`：计划、执行、前后对比；
- `quality_exclusions.py`：使用的脚本；
- `rollback.sql`：回滚语句。
