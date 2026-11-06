# 服务端查询接口候选

本次只处理服务端，不修改安卓工程或APK。开发基线为7930ab0（此前令牌生命周期/查询缓存候选），生产基线为d2e7288。查询功能是增量合并，既有设备令牌、worker绑定、任务归属、小世界、首局身份与冻结算法保留。未引入附件的room_lost调度算法。

## 配置与生命周期

在现有config.json顶层增量增加query对象，默认关闭：

```json
{
  "query": {
    "enabled": false,
    "writes_enabled": false,
    "admin_token": "",
    "token_hours": 24,
    "refresh_days": 180,
    "code_hours": 24,
    "portal_port": 8788
  }
}
```

启用前使用独立的高熵管理令牌（至少24字符，不得复用remote.token）；通过安全渠道写入配置，不放进聊天、日志、URL、Git或导出包。现有remote.enabled仍须开启，本功能复用8791监听；统计读取固定走127.0.0.1上的portal_port，不访问任意远程地址。配置无效时只停用query，不停用watch。关闭query.enabled可回退查询功能；配置改变需要相关进程重新加载。

账号和令牌哈希独立存于data/query-auth.sqlite3，与bot.sqlite3中的观战令牌分开。激活码默认24小时有效、仅用一次；access默认24小时、refresh默认180天；刷新后旧的一对立即失效。登录失败按账号+来源IP组合及来源IP累计，来源IP限流桶分离；并发消费码/刷新使用事务串行化。退出只吊销当前一对，账号重置/停用会作废其全部会话。

## 用户接口

除login和refresh外，用户接口使用Authorization: Bearer查询access令牌。观战令牌、查询令牌和管理令牌用途分离。

| 方法与路径 | 输入 | 返回 |
|---|---|---|
| POST /query-api/login | JSON account（QQ号）、secret（激活码） | ok、identity、role、writes、token、refresh_token及两个expires_at |
| POST /query-api/refresh | JSON refresh_token | 新的一对令牌及身份、权限 |
| POST /query-api/logout | 查询access令牌，无需正文 | ok |
| GET /query-api/me 或 /status | 查询access令牌 | ok、identity、role、writes、device |
| GET /query-api/top100 | 无 | data为既有门户roster结果 |
| GET /query-api/player/名称/pt | 路径名称百分号编码 | data为门户积分历史 |
| GET /query-api/player/名称/history | 同上 | data为门户历史结果 |
| GET /query-api/player/名称/decks | 同上 | data为门户玩家构筑，补齐card.name |
| GET /query-api/player/名称/replays | 同上 | data.items及data.link；逐项url为下载链接，link含目录URL、expires_at、public |
| GET /query-api/charts/pie | 无 | data.items、pie、covered、current、unidentified；与门户前百快照共源 |
| GET /query-api/search?type=player& q=关键词 | 实际URL去掉空格；type支持player/card/deck，q最多80字 | player/card沿门户；deck按当前前百deck_distribution名称检索，最多30项 |
| GET /query-api/card/卡号 | 1～4294967295 | data.details及data.metrics；统计不可用时metrics可为null，卡片信息仍返回 |
| GET /query-api/subscriptions | 本人查询令牌 | data.items，仅本人订阅 |
| POST /query-api/subscriptions | op=subscribe或unsubscribe、player；订阅可带rank_top=1～100整数或null、notify布尔值 | ok及操作结果；未知op、错误类型返回400 |

玩家路径名称最多100字，不能含斜杠；查询不猜首局、不更改统计口径。构筑搜索限定当前前百分布，不承诺历史全库搜索。录像只返回14天内现存文件，每次最多1000条；目录链接按当前查询用户创建，缓存包含用户身份。没有公网分享地址时不向远程用户返回不可用的回环链接。

订阅写入同时要求query.writes_enabled=true与账号writes=true；账号权限变动按请求实时验证。调用方不能通过正文选择其他账号。订阅与QQ使用同一张既有subscriptions表；本次没有新建自动推送任务。录像GET在需要时沿现有分享机制创建/复用share_links记录，不能称为数据库完全只读。

查询读取通常返回{"ok":true,"data":...}，认证/管理/订阅写入直接返回字段。预期错误：400输入无效、401认证失败、403无写权限、404无对应路径、409账号已开通、429限流/繁忙、503查询未启用或上游不可用。/query-api/command保留为已认证请求501，不提供命令文本透传。客户端应使用结构化端点，不能把所有200业务结果都理解为“有数据”。

## QQ开通与管理

- 开通查询与「观战设备」「令牌分发」一样绑定保密名单：优先 `remote.token_groups`/`token_users`，未配置时沿用 `statistics.allowed_groups`/`private_users`；名单外私聊明确拒绝，名单外群静默，群内不返回激活码。帮助清单只对名单内（且有普通使用权限）的人显示这一行，@ 触发的帮助走同一判定。
- 名单内用户私聊“开通查询”创建自己的查询账号并收到一次性码；已有账号再次发送即自助重发（旧待用码作废，不注销已有会话），不再需要管理员手动重置。停用账号不能自助恢复。
- “令牌分发”等现有命令保留。附带恢复“选卡/查卡下一页”入口；构筑卡名附加移入原缓存回调，缓存键、TTL、来源和算法不变。
- 管理UI：通过SSH隧道访问http://127.0.0.1:8791/query-api/admin/ui，在页面输入管理令牌。页面只允许直接回环访问，数据/操作另要求X-Admin-Token或Bearer管理令牌。来自代理的X-Real-IP/X-Forwarded-For/Forwarded请求被拒绝，即使带正确管理令牌也不能经公网代理调用。

| 管理接口 | JSON字段 |
|---|---|
| GET /query-api/admin/list | 无；列账号、设备会话摘要和锁定，不含明文码/令牌 |
| POST /query-api/admin/issue | identity，5～20位数字QQ号 |
| POST /query-api/admin/reissue | identity，重发未使用激活码，不自动注销现有会话 |
| POST /query-api/admin/reset | identity，吊销全部会话并返回新码 |
| POST /query-api/admin/disable | identity，停用账号 |
| POST /query-api/admin/writes | identity、enabled布尔值 |
| POST /query-api/admin/revoke-device | token_id，列表中的会话哈希前缀 |
| POST /query-api/admin/unlock | key，列表中的锁定键 |

新码只出现在发放/重发响应与 QQ 私聊回复中，不应复制到日志。真实QQ发送、正式账号签发和真设备登录未作为本地测试执行。

## 资源边界

query最多同时处理2个请求，繁忙返回429；不占用watch的请求闸门。查询JSON正文最多64KiB，连接读超时10秒，拒绝不完整/非法正文后关闭连接。新增查询缓存最多32条、总编码字节8MiB、单条4MiB，缓存未命中计算串行化；只改变新增query层，不改网页既有缓存。饼图和构筑名称搜索复用roster，避免在collector重复做全库统计。默认禁用订阅写入，部署时再显式开启。

## 部署与回滚

1. 合并候选包含此前尚未上线的令牌生命周期与查询缓存依赖，交付manifest会标注继承文件与本次新增/修改文件；不能只拷贝query_api.py，也不能把附件整个仓库覆盖服务器。旧候选的首次部署排空要求仍适用。
2. 先核实际生产摘要、余量和其他聊天更新，按清单备份受影响源码及配置；不复制大型事实库，不删除SQLite WAL/SHM。源码变动后按清单重新合并。
3. 安装前完成Python编译、相关回归；生成/安装证书是独立操作。tools/gen_query_cert.sh只生成新证书，拒绝覆盖现有证书，不自动安装或重载nginx。
4. deploy/nginx-query-api.conf是独立8443 TLS入口模板，需核实际nginx配置和证书路径后增量合入、nginx -t，再按许可平滑加载。不替换8787。公网屏蔽全部/query-api/admin前缀，应用层也拒绝转发管理请求。
5. 自签证书需要调用方显式信任；本次不修改安卓信任配置，因此不能宣称现有APK已经能连接。服务器接口可以先通过携带该证书的独立客户端验收。
6. 加载涉及collector、QQ、web以及nginx；对应源码归属ygo:ygo。线上重启须用户确认，且避开在录/在途任务。需要watch加载时只运行一次activation-check；状态未知或在录则记待加载，不等待循环、不强制重启。
7. 验收新接口、既有观战设备认证、8787/8788健康及管理公网隔离。摘要匹配不代表进程已加载。回滚恢复备份源码/配置及此前nginx配置；保留新增认证库和任务归属账本留证，不删除录像或事实数据。

## 当前验证边界

本地语法、查询HTTP/认证及真实门户桥接有测试证据；相关广回归的历史失败单独留档。浏览器工具初始化超时，管理UI真实点击仍待验收，JS语法检查已通过。线上配置、TLS安装、服务加载、正式账号及真实设备验收未执行。仅凭本文件不能称为生产已生效。
