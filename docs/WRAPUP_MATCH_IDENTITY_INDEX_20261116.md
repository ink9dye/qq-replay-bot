# 小局身份落表（stat_match_identity）2026-11-16

## TL;DR

- **问题**：卡片统计/录像筛选每次冷查询都要把 `stat_evidence` 三万多条 `payload_json` 解一遍，再做跨会话合并，只为回答“这一局属于哪一场、第几局、是否从开局录”。
- **改动**：新增 `stat_match_identity` 小表，入库时算一次、查询只读表；推导规则仍由 `statistics_match_identity` 负责，判定口径零变化。
- **效果**：`capture_metadata` 全量 **14.0 秒 → 0.49 秒**；线上查询埋点里 `identity_read_ms` 不再出现（`identity_index_hits=59033`）。
- **一致性**：回填后全量核对 30,682 条，与现算结果 **0 条不一致**；抽查 200 条同样 0 条。
- **部署**：2026-11-16 11:48 上线，`ygo-web` 2706992 → 2710308，三服务 active，8787/8788 健康。
- **回滚**：恢复 `bot/statistics_facts.py`、`bot/statistics_match_identity.py` 两个备份文件并重启 web；索引表可保留（旧代码不读）或 `DROP TABLE stat_match_identity`。

## 证据：慢在哪一步

线上只读实测（30,688 个 digest，机器空闲时）：

| 步骤 | 耗时 |
|---|---|
| `json_extract` 取 capture | 0.8s |
| 解析 29,395 条 capture（`json.loads`） | 6.0s |
| 上传台账 `watch-upload-sessions` | 2.0s |
| 旧路径 `stat_files JOIN replay_sessions` | 1.2s |
| 跨会话合并 `_reconcile_capture_sessions` | 1.1s |
| `identity_metadata`（首局确认库） | 0.4s |
| **合计** | **14.0s** |

同一函数在 I/O 压力 72%、swap 吃满时实测 **177.9s**，这就是 544 秒那次卡片统计里的 `identity_read_ms`。线上日志里另有两次：`id=13.95s`、`id=19.07s`。

合并逻辑不能省：全量重算中，跨会话合并改写了 **249 条**、新增了 **201 条**（两个观察者同场时统一到最早场次）。所以不能简单缓存“唯一 capture 元组”，必须缓存合并后的结果。

## 改动

### 1. 新表与失效机制（`bot/statistics_match_index.py`，新增）

```sql
stat_match_identity(digest PK, session_id, game_no, captured_from_start,
                    parent, resolved, computed_at)
```

- `resolved=1` 表示有可靠身份，`0` 表示“算过但没有可靠身份”——后者也要落行，否则每次查询都会回退重算。
- `parent` 存唯一的前一局 digest，用于后续小局的反向刷新。
- 触发器：
  - `stat_match_identity_removed`：`stat_games` 删除（过期/换源）时同步删行，不依赖连接的 `foreign_keys` 开关；
  - `stat_match_identity_stale*`：`stat_evidence` 的 INSERT/UPDATE/DELETE 一律删除对应行，任何绕过 `import_replay` 的写入也不会读到旧身份。

### 2. 入库写入（`bot/statistics_facts.py`）

- `initialize` 里建表建触发器（幂等）。
- `import_replay`：只有证据真的新增（`INSERT OR IGNORE` 的 `rowcount>0`）或该 digest 尚未建索引时才重算，避免全量重扫时每条都算一次。
- 重算走 `refresh`：算完当前小局后，按 `parent` 反查引用它的后续小局一并刷新（深度上限 20，防环），解决“首局比后一局晚入库”的旧问题。
- 失败只记不抛：索引出错不回滚录像事实，查询端会回退到原推导。

### 3. 查询读表（`bot/statistics_match_identity.py`）

`capture_metadata` 改为：先读索引 → 未建索引的 digest 才走 `_read_capture_metadata`。老库没建表时 `lookup` 返回空，行为与改动前完全一致。

### 4. 回填工具（`tools/backfill_match_identity.py`，新增）

```bash
python3 tools/backfill_match_identity.py --root /opt/ygobot [--dry-run] [--verify 200]
```

只读证据推导一次，`rebuild` 批量写表；可重复执行，已建索引的 digest 默认跳过。

## 验证

| 项目 | 结果 |
|---|---|
| 单元测试 `tests/test_match_identity_index.py` | 8 项全过（重复观察者、后一局继承、首局晚到、冲突局次、过期清理、入库写入、证据变更失效） |
| 回归 | `test_statistics_facts` 13 项全过；与旧实现逐用例对比，**没有新增失败**（`test_build_classification` 2、`test_statistics_intermediate` 2、`test_statistics_feature65` 12 为既存失败，与本次无关） |
| 回填 | 30,682 条 / 9.7 秒；有可靠身份 28,845，明确无身份 1,837 |
| 全量核对 | 30,682 条索引值与现算值 **0 条不一致** |
| 线上查询 | `card-report?code=14558127&self_range=200&opponent_range=200` 冷算 50.1s，埋点 `identity_index_hits=59033`、无 `identity_read_ms`；同条件再查 0.01s |
| 写入链路 | 回填后新增 3 条录像，索引行同步增加 3 条（30,682 → 30,685），无需人工干预 |
| 服务 | `ygo-web` 2710308，`ygo-qq`/`ygo-collector` active，8788 `/health` 200 |

## 回滚

1. 恢复 `/opt/ygobot/data/config-backups/match-identity-index-20261116-1150/` 下的 `statistics_facts.py`、`statistics_match_identity.py`（原 SHA 前16：`37489d3dc12b3d38`、`e58ad3ea649ac4ef`）；
2. `systemctl restart ygo-web`；
3. 索引表可保留（旧代码不读），确认无误后再 `DROP TABLE stat_match_identity`。

## 未做 / 后续

- **未提交 `bot/statistics_facts.py`**：工作树该文件还含另一会话未提交的 `_name_imported_builds`（入库即命名），整文件提交会把别人的在途工作带上。本次只提交新模块、`statistics_match_identity.py`、回填工具、测试与本文档；`statistics_facts.py` 的两处 hunk（initialize 处建表、证据写入后 `refresh`）已上线但留在工作树，待那个会话提交后再一并入库。
- 预计算仍会被 guard 跳过（io_pressure/load），冷查询依旧存在；身份只占冷查询的一部分，`eligible_sql_ms`（9.4s）和卡牌聚合是剩余大头。
- 容量仍是硬约束：1740MB 内存 + 1.8GB swap。
- 100/99 个组合上限、卡组候选人数排序等此前遗留项未动。
