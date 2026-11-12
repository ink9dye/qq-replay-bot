# QQ管理员代开通与保密群临时私聊修复（2026-11-12）

## 背景与结论

维护者要求仅QQ 1278389701、3572605808可以代其他QQ开通观战令牌、查询账号，结果私聊给发命令的人；随后反馈1174338198从保密群发起临时私聊时收到名单外提示。

14:36已上线管理员代开通（master `0233e042c7aa54d8`）。本轮核查1174338198个人授权文件及两层权限均为允许，截图所示拒绝尚未在当前版本复现，不能断言截图时间或具体触发原因。另确认临时会话群来源在原事件处理里被丢弃，14:44补齐此路径并上线。

## 最终行为

- 两位指定管理员发送 `令牌分发 QQ号` 或 `开通查询 QQ号`，账号/令牌属于目标QQ，结果仅私聊发命令者。群内仍只在保密群且@机器人触发。
- 目标QQ的令牌/查询自助权限独立保存于 `data/confidential-grants.json`，不保存凭据，不授予代办或其他功能权限。原1–9设备槽位、冷却、重发吊销规则继续使用。
- `message_type=private, sub_type=group` 的临时私聊，若已有相应个人授权则直接允许；否则调用只读 `get_group_member_info(no_cache=true)` 核实保密群成员身份。
- 有明确group_id时只检查该群；无group_id时仅检查配置内的保密群，不猜target_id。成功结果须同时匹配user_id、group_id，每次查询超时3秒。
- 成员核实成功只赋予本次两条命令所需的内部私聊上下文，不持久加白名单，不扩张统计/订阅/管理权限。查询失败、非成员或明确非保密群来源不放行；回复仍走私聊。

NapCat临时私聊事件格式依据：[官方事件说明](https://napneko.github.io/onebot/basic_event)。临时来源不完整时采用实际群成员查询，不能仅凭“临时会话”字样直接放行。

## 验证

- `python -m py_compile bot/service.py bot/watch_token_command.py tests/test_confidential_delegation.py`：通过。
- `python -m unittest discover -s tests -p test_confidential_delegation.py`：18项通过。覆盖代办身份、目标归属、权限持久化/隔离、同槽位冷却、群内结果私发、临时来源缺失/非成员/超时、普通私聊不探测、完整临时私聊事件仅私发等。
- `git diff --check`：通过。
- 代开通阶段查询鉴权21项通过；旧令牌21/23与query core18/19通过，3处既有失败在HEAD基线也复现，详见交接；本次临时私聊修改不再扩大重复测试。
- 生产只读：1174338198的watch_token与query_enroll权限均为True；没有代登录、消费激活码、重新签发令牌或发送无业务目的测试消息。
- 新进程已上线/接收群事件，启动日志无Traceback；真实手机临时私聊回复待用户业务使用确认。

## 部署

生产文件在写入前核对最新LF归一SHA与上一版，再备份并逐字节比对；候选先编译再替换，属主ygo:ygo。

| 文件 | 本次原始SHA256前16 | LF归一SHA256前16 |
|---|---|---|
| bot/service.py | b638f35f88baa538 | fe873b855bb931fd |
| bot/watch_token_command.py | 8478754524e5b580 | 80df9d8f89904f54 |

备份：`/opt/ygobot/data/config-backups/confidential-temp-private-20261112/before/`。

按维护者授权，14:44:31仅重启ygo-qq，PID1932140→1933933；QQ online、failures=0，新PID已接收群事件；web/qq/collector均active，8787/8788 health=200。未重启NapCat/web/collector，未改安卓代码。systemctl仍提示磁盘unit有其他变动，本轮未改unit或daemon-reload。

本地脱敏部署证据：`workflow-results/confidential-temp-private-20261112/deployment.txt`。文档/证据不保存明文激活码、令牌、管理密钥。

## 回滚

恢复上述before目录的service.py、watch_token_command.py，chown ygo:ygo，编译后按授权只重启ygo-qq。此回滚恢复14:36管理员代开通版本，现有账号、令牌、授权文件继续保留，不回滚数据库。若需一并撤回管理员代开通，另使用confidential-delegation-20261112/before的3文件备份；账号/令牌撤销使用原disable/revoke接口。