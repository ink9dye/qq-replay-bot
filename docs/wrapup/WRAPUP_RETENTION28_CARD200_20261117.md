# 28天保留与QQ单卡胜率前200收尾（2026-11-17）

用户授权：延长数据到28天；仅QQ单卡胜率的总体、先后攻、对阵胜率改用双方当前前200。使用量、单卡流向、卡组统计及前百饼图人数选组维持前100。标题改为“对前百卡组胜率最高的5个（使用后）：”，不足5行显示实际数量。

## 实现与影响面

retention_policy统一14→28天，录像/卡组/解析清理与统计新记录沿用该常量；query_core原硬编码窗口改为统一常量，相关网页与QQ文案同步。名次快照原14天新鲜度不改，不调整命名、Side算法、取源与缓存策略。QQ单卡胜率通过现有population_scope('200','200')验证双方，未知名次不纳入；matchups默认前100保持，QQ已验证人口显式启用前200聚合。前百饼图选组仍沿原人口。

已有stat_games存有固定expires_at，新增tools/extend_retention_28.py迁移：默认只读预览；--backup新JSON路径才保存原到期值后写入；只延长尚未到期且符合旧14天规则的记录，重复执行不续期。未恢复已清理数据，不修改原始收录时间。

## 部署与验证

部署前线上14文件逐个按LF归一化核对基线，13份与master HEAD一致；share.py存在已上线的matchup_labels传self.store._db修复，本轮保留并合并。再核对生产SHA无漂移后逐文件备份，录制状态0且状态新鲜，内存可用611MB、磁盘剩余16GB。

备份：/opt/ygobot/data/config-backups/retention28-20261117-2138/，含原文件、old-deadlines.json、deployment.json（完整SHA/PID）。暂停QQ/web/replay-api/collector后替换15份文件、chown ygo:ygo、服务器编译、迁移到期值，再启动四服务；NapCat未动。迁移32,287条；只读复核未过期记录中旧14天到期0条、28天到期32,287条。

PID：collector 2793834→3034841，replay-api 2252934→3034843，web 2992699→3034845，QQ 2937666→3034847。四服务active；8787与8788健康200，8787健康接口retention_days=28。新的QQ模块消息可见前200人口标记；尚未发送测试QQ消息，未声称真实卡片胜率回复或前200全量业务计算验收通过。

本地28项测试通过：test_card_winrate.py 8项（含200纳入、201/未知排除、恢复默认人口）、test_retention_policy.py 5项（含28天精确清理边界）、test_extend_retention_28.py 1项（预览/备份/迁移/幂等）、test_game_winrate.py 11项、test_rank_scope_population.py 3项。命令：python -m unittest discover -s tests -p 对应文件名；修改Python全部py_compile、statistics_build.js的node --check、git diff --check通过。保留期测试移除对已过时HELP_ITEMS测试模块的导入依赖，保留原测试行为。未重复扩大全库检查。

## 回滚与边界

先diff生产最新文件，恢复本轮实际备份并chown；old-deadlines.json保存每条digest及原expires_at，只在当前expires_at等于原值+14天时恢复，避免覆盖后续修改。需重新取得重启授权并避开录制。已新增的28天记录另行按实际保留政策评估，不盲删。新增迁移脚本可留存或删除，不恢复数据库整库备份。

28天数据量会逐日增长，磁盘/数据库/查询成本需观察；目前不能宣称扩容后的长期性能已验收。前200仍依赖既有名次解析与缓存，缺失名次按不合格处理。其他会话未上线修改、安卓代码、凭据均未纳入。证据留在本地.tmp-ops/retention28-*与生产备份，不入库。
## 版本与最终状态

代码及首版收尾已上传master：67ca730f69676d95，远端与本地一致。生产raw SHA前16：retention_policy.py f156872e646564fa；card_winrate.py 173aa3b314b36ae1；card_statistics.py 629aecfcab08358a；迁移脚本3118810221553ad7。提交前15份部署文件再次按LF归一化核对，本地与线上全匹配。QQ状态文件state=online。账号凭据与临时证据未入库。
