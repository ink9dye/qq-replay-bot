# 观战与网页更新边界（2026-11-05）

生产 collector 通过 systemd 的 collector-web-boundary.conf 设置 YGO_STATISTICS_REMOTE_ONLY=1。这是生产部署选项；未设置时保持原有本地单进程用法。

| 业务 | 执行服务 | 后续正常更新重启范围 |
|---|---|---|
| 房间发现、等待/派发、claim/report/heartbeat、录像/旁证上传与入库 | ygo-collector（8791接口，8789兼容入口） | 改这些才重启collector |
| 统计后台、公开统计链接查询、录像分享页/构筑/分析页面与下载 | ygo-web（8788） | 网页Python模块重启web；静态文件通常下次请求即读取 |
| QQ消息接收、命令、回复、分享链接生成 | ygo-qq | QQ相关代码重启qq |
| 8787公网入口与路径转发 | nginx | 改转发时nginx检验后reload |

collector 的 statistics_gateway 只签本机会话、转发请求，不实例化统计后台。公开统计链接继续在collector做轻量权限检查后转发；公开录像链接在web用只读Store核权限并渲染。web不可用返回503，不回退到collector计算，不转移或取消观战任务。入口、链接格式、过期/撤销规则、冻结的卡图/缓存/命名/构筑算法不变。网页解析请求仍可能更新既有分析缓存，不作“全业务只读”的承诺；Store连接只读且不迁移。

这是进程职责分离，并非整机资源隔离：仍共享SQLite、磁盘、CPU/内存；更新共同被观战入库使用的底层模块、数据库结构、gateway或任务/上传接口，仍可能需要collector重启。部署前依据实际调用关系选择服务，不再以“任何统计文件变化”默认三服务重启。

第一次切换需collector和web各加载一次。以后验收web更新：记录collector PID与启动时间、watch-api/status；仅重启web；核collector PID和启动时间不变、watch-api仍响应，再核网页恢复。web短暂停止时不制造真实任务或上传测试录像污染业务库。生产验收记录维护在PROJECT_HANDOFF.md所指证据路径。

设备页“等待认领”按用户口径等于QQ“等待观战”：取data/capture-status.json的waiting_rooms，采用同样90秒有效期。任务板pending另留为task_board_pending，不混用。采样缺失/过期显示“—”；页面最多5秒快照缓存和自动刷新间隔意味着两次非同时查询可以略有时间差。

回滚：还原本次运行文件，删除或还原本次新增drop-in，daemon-reload后重启collector/web；不要卸载客户端或改配置/业务库。部署清单和真实字节备份以生产记录为准。
