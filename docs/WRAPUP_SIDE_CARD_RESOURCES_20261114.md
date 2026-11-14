# Side 缓存连接与查卡跨用户阻塞修复（2026-11-14）

## 背景与改动

修复已复现的两项P2。side_cache 在读取、计数、提交和初始化失败时显式关闭连接，保留事务提交/回滚和已有键不覆盖。card_search 网络、重试和图片等待移出共享状态锁；同用户操作串行，同搜索及同卡发售刷新合并；空闲锁及时释放。未改取源、缓存时效、Side推断或统计口径，无新增依赖。

## 版本与验证

线上原始SHA前16：side_cache 105874e5772c9748 → 7b835acf0eadd46f；card_search 5217520b0d5e76e8 → f8a6934e0b450414。完整原始/LF SHA见工作区 workflow-results/side-card-fix-20261114/manifest.json。

py_compile、git diff --check通过。新增tests/test_side_card_resources.py的12项测试，在Windows Python3.14及服务器Python3.12.3隔离环境均通过：关闭GC重复读取/计数连接全关闭、初始化/JSON异常、失败回滚、并发提交、跨用户候选/翻页/独立查询、图片/发售等待隔离、同用户purpose、同请求合并与锁释放。30项旧回归受控对照，基线和候选均28通过、相同2失败：旧离线提示断言、群聊异步首条回复断言；未称全量通过。

## 部署与影响面

维护者本轮明确授权部署、重启、上传与收尾。部署前重新核对线上原始SHA和改前本地LF一致，实际备份并逐字节校验；active_observations=0且状态新鲜时替换两文件并chown ygo:ygo。备份 /opt/ygobot/data/config-backups/side-card-fix-20261114-085923/。

仅重启ygo-web、ygo-qq、ygo-collector，PID分别2277150、2277147、2277149，全部active；8787/8788 health均200，文件SHA匹配候选。未重启NapCat，未daemon-reload，未改数据库、安卓或其他会话代码，未主动发QQ测试消息。真实QQ指令回复及长期资源表现仍待日常使用验收。

## 回滚

先diff最新生产，保全后续修改，再恢复上述部署时实际备份的两文件，chown ygo:ygo、编译并经授权在无录制窗口重启受影响服务，复核健康及QQ状态。不恢复数据库或缓存数据，不重启NapCat。

## 教训

SQLite连接的with只处理事务；必须外层closing、内层事务，初始化异常也要关闭。共享状态锁不可覆盖网络等待；拆锁必须保留同用户会话一致性和同请求合并。并发测试用事件控制等待，避免外网时延假象；旧测试失败须对照生产基线，不能为了绿灯改业务语义。
