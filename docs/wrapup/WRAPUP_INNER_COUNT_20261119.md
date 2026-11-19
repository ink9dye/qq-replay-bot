# QQ 与网页前 200 内战计数统一（2026-11-19）

QQ「统计对局 前200 内战」改用网页卡报已有的 `top100.eligible_views` 和 `statistics_rank_scope`。两端共用录像资格、双方名次判断和指纹去重。QQ 同时显示去重小局、玩家视角、北京时间今日首次入库及条件未判定的小局，冷查进现有统计队列。网页卡报新增去重小局数，与原玩家视角和胜率样本并列。网页的卡组、先后攻和其他筛选仍作用于该页结果。同排名、快照和筛选才可直接对照。此次范围为前 200 双方内战。

同一测试库和名次快照下两端均为 5 局、10 视角。覆盖 200/201 边界、未知名次和重复入库。QQ 小局计数 9 项、排名范围 9 项、卡片资料 11 项及线上 Python 3.12 专项 3 项通过，Python/JS 语法通过。投影与中间缓存旧测试共 9 条失败断言在本轮线上基线同现。服务器专项初次缺两份测试夹具，补齐后通过。未主动发 QQ 测试消息；网页资源匿名请求返回 403，浏览器登录态实用验收待完成。

零录制、无 QQ 在途且 Web 刷新 worker 空闲时，QQ 3322723 → 3326722，Web 3319428 → 3326724，QQ 确认在线，8787/8788 均 200。六个生产文件重新比对一致。SHA256 前 16 位为 game_counts.py `50abb9db969d39ec`、service.py `5642a83b2da53258`、statistics_card_report.py `d1329380b980c4ac`、statistics_rank_scope.py `b9498abcbcc8b4bd`、statistics_cards_report.js `38c96d70070f9373`、statistics_cards_report.html `e371d9e8fbdb95b1`。

回滚先比对线上最新文件，恢复 `/opt/ygobot/data/config-backups/inner-count-20261119/` 六份原件及属主，经授权在空闲时重启 QQ 与 Web，核对新进程和健康。无需数据库回滚。
