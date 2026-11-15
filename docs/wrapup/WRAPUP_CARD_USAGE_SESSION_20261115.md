# 单卡使用量漏人修复收尾（2026-11-15）

已部署，修复范围仅 `bot/card_usage_pie.py` 的选场及标题。首局缺失原因仍为待办T02，未改采集、Side推断、命名或安卓。

## 问题与改动

旧实现取绝对最新场再检查首局，导致已有较早可靠首局的玩家被排除。新增 `_selected_sessions`，以共用 `population.latest` 已选首局所属session及双方身份匹配后续局；无session时仅按digest匹配，不跨场推断。主卡直接使用共用已选构筑，重复首局按共用结果处理。未改变 `_latest_sessions`，避免影响 `card_flow`。标题改为“有效100/100人（排除0人）”，底注明确所选同场比较。

## 验证

- 本机Python3.14：`python -m unittest tests.test_card_usage_selected_session tests.test_card_code_resolve.ResolveCardByDataTests`，8/8；`py_compile`通过。
- 服务器Python3.12，隔离加载候选、复用生产依赖：新增4项全过，覆盖最新场缺首局、跨场隔离、重复首局、完全缺首局与digest兜底。
- 线上只读同批人口数据：旧96、新100、卡组饼图100；主卡分布52/31/11/6，备牌48/30/21/1，总投入19/30/37/14，三图均合计100。预览PNG 106905字节，已视觉检查标题及布局。
- 初轮广泛测试19项有5个错误：新增fixture的Windows连接未关闭3项已修正并通过；旧 `UsageMergesPrintsTests` 缺 `stat_roster_state`，旧profile导入 `test_bot` 路径失败，未声称全量通过。另一次本机调用工作目录错误，随后在开发目录正确运行8/8。

## 部署与回滚

用户明确授权部署/上传/重启，并临时授权额度83%至91%。线上原始SHA256前16 `a6f120fa37aaa695`；部署前核对完整SHA，备份后逐字节比较，再次确认未变后替换、chown ygo:ygo、644及编译。

备份：`/opt/ygobot/data/config-backups/card-usage-session-20261115/card_usage_pie.py`。

候选与部署文件原始SHA256：`958b1f2223620dc109558b6f1e896f12cf0924cd19575bea42c29c0a163419c0`（LF文件）。只在 `active_observations=0` 重启web、QQ：PID2562561→2567629、2526629→2567631；collector2566914不变，NapCat未动。三服务active、8787/8788健康200，QQ新进程日志确认连接并接收事件。未发QQ测试消息，实际业务回复由用户下一次查询验收；独立预览不冒充真实QQ端到端验收。

回滚：先比对届时线上最新文件与本次候选，保留他人改动；恢复实际备份、chown、编译，经授权在无录制窗口重启web/QQ，再查健康与QQ连接。不恢复数据库。

## 协作与教训

开发库已有他人QQ观测候选。本次从生产基线生成最小候选；开发文件合入修复但保留观测改动，Git索引仅写入实际部署版本，其他未完成修改不提交。交付证据在工作区 `.tmp-ops/card-usage-session-20261115/`，不入库。

共用口径必须复用“选中的场次”，只共用玩家名单仍可能分裂；过滤后的分母不能冒充原榜单人数。后续根因排查继续见待办总表T02。
