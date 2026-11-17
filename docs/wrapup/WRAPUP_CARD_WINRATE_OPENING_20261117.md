# QQ单卡胜率拆分使用与起手结果（2026-11-17）

## 需求与实现

用户授权最多6%额度完成修改、部署、上传与收尾。本轮起读数60%，按最多66%止线执行（账户整数读数）。单卡胜率分别发送【实际使用】和【起手抽到】两条结果，均含总体、先/后攻、胜率Top5及前百饼图人数Top5，双方当前前200。使用量、单卡流向、卡组及前百饼图选组仍按前100，28天保留期不改。

使用沿原used=1；起手使用现有身份归一事实initial_count>0，抽到但未使用也纳入，只有过程抽到不纳入；initial_count未知不纳入，平局进分母，多张起手仍按一个小局视角计一次。不改事实提取、数据库、命名或Side算法。

仅修改bot/card_winrate.py与bot/service.py：复用一次人口、卡片事实、对手与热门卡组读取，两套结果分别在内存聚合；沿现有TextPages慢任务按使用→起手顺序发送。卡片综合资料保留两个独立节点；直接命令、选卡后继续均走同一回复。两条结果在同一次统计完成后依次发送，不声称能缩短首条等待时间，也未延长30秒缓存。

## 验证

本地33项通过：test_card_winrate_opening.py 3项、test_card_query_profile.py 11项、test_card_winrate.py 8项、test_game_winrate.py 11项。新增用例覆盖起手未使用、过程抽到、未知起手、平局、先后攻、一局多张不重复、双条发送顺序、卡片资料双节点，以及人口/事实/对手/热门组各读一次。Python py_compile及git diff --check通过。新增fixture曾因Windows sqlite连接未关闭导致临时目录清理失败，明确close后3/3通过。

服务器Python3.12运行新增3项测试全部通过；未向QQ发送无业务目的测试消息，未发全库业务重查询，真实群回复与耗时等待用户使用验收。原67秒计算路径仍存在，本轮没有宣称提速。

## 部署

生产两文件先与HEAD按LF归一化核对一致；上传后再次核对无漂移，备份到 /opt/ygobot/data/config-backups/card-opening-20261117-2210/。文件raw SHA前16：card_winrate.py 951b606f83a4b387；service.py 52aad1845f6fba55。部署后本地/生产LF归一摘要完全一致。

录制为0、状态新鲜、QQ无在途background_job，可用内存514MB；只覆盖两文件、chown ygo:ygo、服务器编译和3项专项通过后重启QQ。collector/web/replay-api/NapCat未重启，健康200且保留期28天。QQ首次启动遇sqlite3.OperationalError: database is locked（Store初始化top100表），systemd已自动重试；最终在线状态以本文追加记录为准，不能用旧qq-status.json的online冒充新进程验收。

## 回滚

先diff线上最新文件，恢复本轮备份的两文件并chown，再经授权于无录制/在途任务窗口仅重启ygo-qq。无需数据库回滚。不得用旧工作树覆盖其他聊天的新改动。其他会话脏文件、安卓与凭据未纳入提交。
## 最终运行确认

QQ自动重试后新进程3036816已在线，qq-status.json的process_id与systemctl MainPID完全一致，state=online、SubState=running、NRestarts=1；不是旧状态文件。其他三服务active。旧QQ PID3034847；首次启动3036641因写锁失败后自动恢复。

代码及首版收尾提交：aaf1ee93eb502669，已push master，远端一致。本轮用量起60%，部署核验时62%；最终读数另记交接（账户整数百分比，非精确任务计费）。
