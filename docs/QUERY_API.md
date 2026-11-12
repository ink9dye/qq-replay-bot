# 服务端总接口文档

本文是服务端对外接口的统一契约入口，包含查询、录像库及统计网页录像筛选接口。参数和返回结构在本文维护，上线过程与回滚保留在收尾文档。

快速定位：[查询总表](#2-用户接口查询接口) · [管理接口](#3-qq-开通与管理查询接口) · [录像库](#7-录像库接口replay-api只读2026-11-06-上线) · [绑定/换备/单卡统计](#10-绑定换备与单卡统计2026-11-12已上线)。

| 接口 | 用途 | 章节 |
|---|---|---|
| `/query-api` | 查询接口（前百、玩家、构筑、单卡、录像、订阅、绑定、换备），8443 TLS入口 | 1–6、10 |
| `/replay-api` | 录像库：按对局检索/整场下载，PC 客户端 v1.5 使用，8787 入口 | 7 |
| `/statistics/api/recordings` | 统计网页「录像筛选」接口，8787/8788 | 8 |

观战接口 `/watch-api`（客户端领任务/上传）见 `docs/REMOTE_WATCH.md`；
每设备令牌的签发/吊销见 `docs/WATCH_DEVICE_TOKENS.md`。

## 历史说明（2026-11-05）

本次只处理服务端，不修改安卓工程或APK。开发基线为7930ab0（此前令牌生命周期/查询缓存候选），生产基线为d2e7288。查询功能是增量合并，既有设备令牌、worker绑定、任务归属、小世界、首局身份与冻结算法保留。未引入附件的room_lost调度算法。

## 1. 配置与生命周期（查询接口）

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

## 2. 用户接口（查询接口）

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
| GET /query-api/player/名称/decks | 同上 | data为门户玩家构筑（`cards` 含 `partition=main/extra/side`，补齐 `card.name`）；Side 为推测，标 `inferred:true`，并附 `side_inferred` |
| GET /query-api/player/名称/replays | 同上 | data.items；逐项url为认证下载/短期签名链接，当前link=null |
| GET /query-api/charts/pie | 无 | data.items、pie、covered、current、unidentified；与门户前百快照共源 |
| GET /query-api/search?type=player& q=关键词 | 实际URL去掉空格；type支持player/card/deck，q最多80字 | player/card沿门户；deck按当前前百deck_distribution名称检索，最多30项 |
| GET /query-api/card/卡号 | 1～4294967295 | data.details及data.metrics；统计不可用时metrics可为null，卡片信息仍返回 |
| GET /query-api/subscriptions | 本人查询令牌 | data.items，仅本人订阅 |
| POST /query-api/subscriptions | op=subscribe或unsubscribe、player；订阅可带rank_top=1～100整数或null、notify布尔值 | ok及操作结果；未知op、错误类型返回400 |
| GET /query-api/replay/{id} | 查询access或有效短期签名 | 录像二进制下载，见本节下载说明 |
| GET /query-api/binding | 当前查询身份 | data.player；未绑定为null，见§10.2 |
| POST /query-api/binding | JSON player | data.player/known/changed，见§10.2 |
| POST /query-api/binding/unbind | 无正文 | data.removed；重复解绑false，见§10.2 |
| GET /query-api/player/{name}/side-plan | 玩家名及筛选参数 | 换备先后攻分列，见§10.3 |
| GET /query-api/card/{code}/usage | limit=1..100，默认20 | 使用量与裁剪后的records，见§10.4.2 |
| GET /query-api/card/{code}/flow | 不支持limit | 完整卡组流向分组，见§10.4.3 |
| GET /query-api/card/{code}/winrate | limit=1..20，默认20 | 总体、先后攻、对阵Top20，见§10.4.4 |

玩家路径名称最多100字，不能含斜杠；查询不猜首局、不更改统计口径。构筑搜索限定当前前百分布，不承诺历史全库搜索。录像只返回14天内现存文件，每次最多1000条。当前逐项URL为绝对/query-api/replay/{id}下载地址；可带查询access下载，也可使用列表签发的exp/sig短期签名（默认5小时）供浏览器打开。不依赖public_base_url，不返回回环地址，不创建share_links；link保留为null。签名URL本身是临时下载凭证，不记日志或当永久链接保存。

订阅写入同时要求query.writes_enabled=true与账号writes=true；账号权限变动按请求实时验证。调用方不能通过正文选择其他账号。订阅与QQ使用同一张既有subscriptions表；本次没有新建自动推送任务。当前录像列表/下载不再创建share_links；身份绑定及订阅仍是写操作，不将整个query-api称为数据库完全只读。

查询读取通常返回{"ok":true,"data":...}，认证/管理/订阅写入直接返回字段。预期错误：400输入无效、401认证失败、403无写权限、404无对应路径、409账号已开通、429限流/繁忙、503查询未启用或上游不可用。/query-api/command保留为已认证请求501，不提供命令文本透传。客户端应使用结构化端点，不能把所有200业务结果都理解为“有数据”。



> decks 的 Side 说明（2026-11-06 23:40）：stat_build_cards 没有真实 side 分区，cards 里的 partition=side 来自构筑页同一套推测（statistics_build_detail.infer_side）：同场多局差异 + 收敛，每张带 inferred:true、copies、sources、name；顶层 side_inferred={complete,conflict,reason,order} 说明是否完整、是否冲突、原因和排序。completeness.side 仍为 false（无直接记录），不要把它当真实 Side。

## 3. QQ 开通与管理（查询接口）

- 保密群临时私聊：两条保密命令在无个人授权时核实实际保密群成员，成功后仅本次私聊放行；不把临时会话直接当成普通私聊，也不持久扩大权限。见收尾文档 WRAPUP_QQ_DELEGATION_TEMP_PRIVATE_20261112.md。

- 2026-11-12 管理员代开通：仅 QQ `1278389701`、`3572605808` 可发送 `令牌分发 QQ号` 或 `开通查询 QQ号`。令牌归目标QQ所有（默认客户端 `android-QQ号`、槽位1），查询账号使用目标QQ号；结果私聊发给命令发送者，不发给目标QQ、不回显到群。群内仍须在原保密群且@机器人。目标QQ对应的自助领取/重发权限分别持久保存在 `data/confidential-grants.json`（不含凭据），不会互相开通、不会授予代办权或扩大其他功能权限。`令牌分发 2` 等原设备槽位语法保留；同槽位重发沿用原冷却与吊销规则。已停用查询账号仍不能借代办恢复。

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

## 4. 资源边界（查询接口）

查询分轻/重闸门，默认light_gate=6、heavy_gate=2，等待默认0.5/1.5秒；参数可配置，按实际配置生效，不将所有query合称最多2路。繁忙返回429，不占watch闸门。JSON正文最多64KiB，连接读超时10秒；缓存最多32条、总编码8MiB、单条4MiB，同键未命中复用既有单飞，不同键可并行，不宣称跨进程全局单飞。统计上游调用默认8秒超时，冷查可暂时503；新端点细则见§10.1。

## 5. 部署与回滚（查询接口）

1. 合并候选包含此前尚未上线的令牌生命周期与查询缓存依赖，交付manifest会标注继承文件与本次新增/修改文件；不能只拷贝query_api.py，也不能把附件整个仓库覆盖服务器。旧候选的首次部署排空要求仍适用。
2. 先核实际生产摘要、余量和其他聊天更新，按清单备份受影响源码及配置；不复制大型事实库，不删除SQLite WAL/SHM。源码变动后按清单重新合并。
3. 安装前完成Python编译、相关回归；生成/安装证书是独立操作。tools/gen_query_cert.sh只生成新证书，拒绝覆盖现有证书，不自动安装或重载nginx。
4. deploy/nginx-query-api.conf是独立8443 TLS入口模板，需核实际nginx配置和证书路径后增量合入、nginx -t，再按许可平滑加载。不替换8787。公网屏蔽全部/query-api/admin前缀，应用层也拒绝转发管理请求。
5. 自签证书需要调用方显式信任；本次不修改安卓信任配置，因此不能宣称现有APK已经能连接。服务器接口可以先通过携带该证书的独立客户端验收。
6. 按实际文件归属加载对应服务，不默认同时重启collector、QQ、web或重载nginx；对应源码归属ygo:ygo。线上重启须用户确认，且避开在录/在途任务。需要watch加载时只运行一次activation-check；状态未知或在录则记待加载，不等待循环、不强制重启。
7. 验收新接口、既有观战设备认证、8787/8788健康及管理公网隔离。摘要匹配不代表进程已加载。回滚恢复备份源码/配置及此前nginx配置；保留新增认证库和任务归属账本留证，不删除录像或事实数据。

## 6. 验证状态与边界（查询接口）

2026-11-12的7个新增端点已部署到生产：Python3.12隔离48项通过，公网TLS/临时测试身份绑定共享读取、卡牌三接口和换备、Top20与Top5的统计一致性均验收通过。健康8787/8788为200，web/collector已加载，QQ/NapCat未重启。完整证据与回滚见[上线收尾](WRAPUP_QUERY_API_ANDROID_20261112.md)。

这不代表安卓实际UI、真实QQ消息发送、管理UI点击已全部验收。旧查询76项中的7项失败在改前线上基线同样复现，未当作本次新回归；明细见收尾证据。§5描述一般部署流程，历史候选版本、首次启用说明不作为当前运行状态。

## 7. 录像库接口（`/replay-api`，只读，2026-11-06 上线）

### 7.1 定位

给观战客户端（PC / 安卓 / 脚本）检索和下载服务器上**已保存的全部录像**，补齐原来只能靠分享链接（`/s/<token>`，最长 5 小时）看录像的缺口。

**v2（2026-11-06 晚，现行）按“对局”组织和下载**：一场对局 = 一个 session，包含多局；每局带双方、胜负、卡组名。7.12 是 v2 的契约；7.3～7.6 的单份接口是 v1，保留兼容但客户端已不再使用。

只读：数据库以 `mode=ro` 打开，不写库、不写录像目录、不领取观战任务；独立进程，不重启 collector。

### 7.2 入口与鉴权

| 项 | 值 |
|---|---|
| 公网入口 | `http://<服务器>:8787/replay-api/`（nginx 反代到 `127.0.0.1:8792`） |
| 直连 | 仅回环 `127.0.0.1:8792`，不对外 |
| 鉴权 | `Authorization: Bearer wt_...`，校验 `watch_tokens`（只存 SHA-256，过期/吊销即 401） |
| 令牌来源 | QQ 群/私聊发「令牌分发」（保密名单内），回复含服务器地址、接口令牌、客户端名称、有效期；`令牌分发 吊销 <客户端名称>` 立即失效 |
| 传输 | 仍是 HTTP（与 8787 一致），没有 TLS |

令牌等于**全库读权限**：拿到就能检索、下载 14 天保留期内的所有录像（按 2026-11-06 用户选择）。只私发，不进群、不进日志、不进 URL。

### 7.3 `GET /replay-api/ping`

用于客户端“测试连接”。

```json
{"ok": true, "worker": "android-123456789"}
```

`worker` 是该令牌绑定的客户端名称，只用于确认令牌匹配，不参与检索权限判断。

### 7.4 `GET /replay-api/search`（v1 兼容）

| 参数 | 类型 | 说明 |
|---|---|---|
| `player` | 字符串，≤64 字 | 包含匹配；同时匹配“该玩家视角”和“对局双方”。留空=全部 |
| `from` | 日期 `2026-11-06` | 起始日（本地时区 Asia/Shanghai，含当天 00:00） |
| `to` | 日期 | 结束日（含当天 23:59:59） |
| `page` | 整数 ≥1 | 默认 1 |
| `page_size` | 整数 1～100 | 默认 30，超过 100 按 100 |

排序：`recorded_at` 倒序。**总数和条目都按录像内容（digest）去重**：同一份录像被多个玩家视角各存一份时只出现一条，`focus` 里列出所有视角。

```json
{
  "ok": true,
  "page": 1,
  "page_size": 30,
  "total": 13200,
  "items": [
    {
      "digest": "c3520bfb…",
      "id": 14930,
      "recorded_at": 1793940116.0,
      "saved_at": 1793940500.0,
      "players": ["kouta007", "ZreaKenny"],
      "focus": ["ZreaKenny"],
      "session": "4f2a9c…",
      "game_no": 2,
      "name": "124156_c3520bfb_kouta007_vs_ZreaKenny.yrp",
      "size": 688,
      "available": true,
      "download": "/replay-api/download?id=14930"
    }
  ]
}
```

字段说明：

- `recorded_at`／`saved_at`：Unix 秒；`saved_at` 可能为 0（旧记录）。
- `session`／`game_no`：同一整场的标识和小局号；无法判定时分别为 `""` 和 `0`，**不代表第 1 局**。
- `size`：服务器上文件当前字节数；`available=false` 表示文件已删或路径不合法，不能下载。
- `download`：相对路径，拼接服务器根地址后仍是同一个 `/replay-api/` 前缀。

### 7.5 `GET /replay-api/download?id=<replay-id>`（v1 兼容）

返回原始 `.yrp` 字节：

- `Content-Type: application/octet-stream`，带 `Content-Length`；
- `Content-Disposition: attachment; filename="replay.yrp"; filename*=UTF-8''<百分号编码的原文件名>`；
- 文件必须是 `data/replays/` 目录内的 `.yrp`；越界路径（`..`、绝对路径、其他后缀）一律 404。

### 7.6 错误码

| HTTP | error | 含义 |
|---|---|---|
| 400 | `bad_date` | 日期格式不对，或 `from` 晚于 `to` |
| 400 | `bad_id` | `id` 不是整数 |
| 401 | `unauthorized` | 没带令牌、令牌过期或已吊销 |
| 404 | `not_found` | 路径不属于 `/replay-api/` |
| 404 | `file_missing` | 记录在库但文件已删／越界／不可读 |
| 500 | `search_failed` / `resolve_failed` | 数据库或文件系统意外错误（已记一行日志，不含令牌） |
| 503 | `busy` | 检索并发已满（2 路）且等待超过 5 秒 |

### 7.7 资源与安全边界

- 检索并发 2，超过等 5 秒返回 503；单次最多 100 条，避免一次拉全库。
- nginx 侧使用独立 `zone=replay`（2026-11-10 起：20r/s，burst 60，原先复用 `zone=api` 5r/s burst 20，逐场下载会 429）；8787 出站只有 3M，批量下载会占满带宽。
- 只读连接 `file:...?mode=ro`，不持有写事务；不写 `data/replays/`、不删录像、不改保留期。
- 日志只记启动、检索失败类型和文件解析失败，不输出令牌、账号、完整路径。
- 数据保留期仍由既有 14 天策略决定：过期录像既不在库也不在盘，检索不到属正常。

### 7.8 服务端部署（本次实际执行的步骤）

1. `bot/replay_api.py` → `/opt/ygobot/bot/replay_api.py`（`ygo:ygo 644`，`python3 -m py_compile` 通过）。
2. `/etc/systemd/system/ygo-replay-api.service`：`User=ygo`，`ExecStart=/usr/bin/python3 -m bot.replay_api --root /opt/ygobot`，`Restart=always`；`enable --now` 后监听 `127.0.0.1:8792`，日志 `[录像库] 127.0.0.1:8792 已启动（只读）`。
3. nginx 站点 `sites-enabled/ygobot` 在 `location / { return 404; }` 之前插入：

```nginx
location ^~ /replay-api/ {
    limit_req zone=replay burst=60 nodelay;
    proxy_pass http://127.0.0.1:8792;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Connection close;
    proxy_buffering off;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    client_max_body_size 1m;
}
```

`nginx -t` 通过后 `systemctl reload nginx`；**没有重启 collector / web / qq / NapCat**，collector MainPID 前后一致（366743）。

4. 可选配置（不写也行，默认值即生产值）：

```json
{"replay_api": {"bind_host": "127.0.0.1", "bind_port": 8792}}
```

`data_dir` 沿用顶层配置；数据库固定为 `<data_dir>/bot.sqlite3`，录像目录为 `<data_dir>/replays`。

### 7.9 验收记录（2026-11-06，生产）

- 无令牌/错令牌：直连 8792 与经 8787 均为 401；`/replay-api/nope` 404；`/watch-api/status` 仍 401（互不影响）。
- 临时令牌（测完删行）：`ping` 200；`search` 返回 `total=13200`，直连 0.48 秒、反代 0.22 秒；`download` 688 字节，魔数 `yrp2b`；按玩家过滤 `total=37`。
- 服务：`ygo-replay-api` active、`NRestarts=0`；8787/8788 `/health` 均 200；collector PID 未变。
- 客户端：MoeCardWatch v1.1「录像库」窗口本地端到端通过（检索 3 条、下载校验、表格渲染）。

### 7.10 回滚

```bash
systemctl disable --now ygo-replay-api
rm /etc/systemd/system/ygo-replay-api.service /opt/ygobot/bot/replay_api.py
systemctl daemon-reload
cp /root/nginx-backups/ygobot.bak-replay-api-20261106-125841 /etc/nginx/sites-enabled/ygobot
nginx -t && systemctl reload nginx
```

回滚不影响观战、录像、统计库；`/replay-api/` 恢复成 404。备份文件不要放在 `sites-enabled/`（会被 nginx 当配置加载）。

### 7.11 客户端对接

- PC：MoeCardWatch v1.1 右上角「录像库」，按玩家/日期检索、多选下载到 `data\downloads\<玩家>\`；实现见 `clients/MoeCardWatch/app/replay_api_client.py` 与 `replay_browser.py`。
- 其他客户端：只需要实现 7.3～7.5 三个 GET，令牌用现有的 `wt_`，无需新登录流程。
- 不建议在还没做 TLS 的前提下把令牌放进浏览器页面或公共网络请求。

### 7.12 v2：按对局检索和整场下载（2026-11-06 晚，现行）

#### 数据口径

- **对局**：`replay_sessions.session_id` 相同的多份录像合成一场；`game_no` 是第几局。没有 session 的老录像（`prefix:` 或空）按单局成场，`session` 为空。
- **每局的胜负/卡组**：`stat_views`（`player`、`outcome`、`build`）+ `stat_builds.deck_type`（卡组名）+ `stat_build_series`（系列）。不看 `.evidence.json` 也能给出胜负和卡组；刚入库、统计还没算完的对局会短暂为空，刷新即可。
- **下载单位是一场比赛**，zip 内是一个文件夹，文件夹名 `日期_玩家A_vs_玩家B`（**不带卡组名**，卡组在 .yrp 和 JSON 里）：

```
20261106-1439_甲_vs_乙/
  1_…….yrp              第 1 局录像
  1_game.json           第 1 局：winner、winner_index、players、decks、outcomes、captured_from_start、source
  2_…….yrp / 2_game.json
  match.json            整场：key、session、players、decks、score、winner、start/end、games[]
```

`?raw=1` 时额外附带原始 `.yrp.evidence.json`（40KB 级，含完整事件流；默认不带）。

#### `GET /replay-api/matches`

参数：`player`（我方玩家名，包含匹配，≤64 字）、`opponent`（对手玩家名，包含匹配，≤64 字，可与 `player` 同时填）、`self_deck`（我方卡组，包含匹配）、`opponent_deck`（对手卡组，包含匹配）、`deck`（旧参数，任意一方卡组）、`self_rank` / `opponent_rank`（`all` 或 `100`，当前榜单前100）、`from`/`to`（`2026-11-06`，本地时区）、`page`、`page_size`（1～100，默认 30）。

玩家名语义：`player` / `opponent` 都填时，要求这一场里存在两个**不同**的玩家分别匹配（谁在哪边都行）；只填一边就按该名字过滤任意一边。

玩家名统一做**去首尾空白**归一（2026-11-10）：录像头/上传账本里偶有带尾随空格的签名，不做归一会把同一个人拆成两方，出现 `A vs B vs A`、比分 `0:0:2`、卡组三份的错误展示。

上传账本里的名字还可能是 URL 编码（`+` 代表空格，如 `久+远` vs 证据里的 `久 远`）：归一以本场 `stat_views.player` 的原名为准，能解开的就用原名写法，避免同一个人被拆成两方。2026-11-10 全量复核 9,710 场：无尾随空格、无重复边、无三方、比分/胜者/卡组键均一致。

卡组语义：`self_deck` 只看“我方”的卡组、`opponent_deck` 只看“对手”的卡组；**以对战为单位**取该场该方展示的卡组，按**子串、忽略大小写**匹配（不逐局比较，避免换 Side 后同一套卡组被判定成不同名字而误命中）。角色和排名筛选一致（我方=player 或左方，对手=opponent 或另一名玩家）。`deck` 保留旧的“任意一方”行为。

排名筛选语义：填了 `player` 时“我方”=该玩家、`opponent` 时“对方”=该对手；未填时按席位——我方=`players[0]`（左方）、对方=`players[1]`（右方）。两个都填 `100` 即“前100内战”。榜单取 `stat_current_top100`（当前名单），不是录像当时的排名。

```json
{
  "ok": true, "page": 1, "page_size": 30,
  "total": 8202, "total_bytes": 6500000,
  "items": [
    {
      "key": "s:f00822f6…", "session": "f00822f6…",
      "players": ["1板蓝根", "系统问题"],
      "decks": {"1板蓝根": "耀圣", "系统问题": "珠泪"},
      "score": "1:1", "winner": "", "decided": 2,
      "start": 1793940116.0, "end": 1793940716.0,
      "game_count": 2, "bytes": 1400, "available": true,
      "folder": "20261106-1200_1板蓝根_vs_系统问题",
      "download": "/replay-api/match.zip?key=s%3Af00822f6…",
      "games": [
        {"id": 14930, "digest": "…", "game_no": 1, "name": "…yrp", "size": 688,
         "evidence": true,
         "views": [{"player": "1板蓝根", "outcome": "win", "deck": "耀圣", "series": ["耀圣"]}],
         "download": "/replay-api/download?id=14930"}
      ]
    }
  ]
}
```

`total_bytes` 只算 `.yrp`（当前全库约 6.5 MB）；`score` 只在双方胜负都已知时给出，否则为空串；`bytes` 为 0 表示这场文件已删。

#### `GET /replay-api/match?key=<key>`

返回单场详情（结构与列表项一致）。404 `match_missing`。

#### `GET /replay-api/match.zip?key=<key>[&raw=1]`

返回整场 zip（`Content-Type: application/zip`，带 `Content-Length`）。404 `match_missing`。

#### 带宽与并发（3 Mbps 出口）

- 所有下载（单份/整场）共享一个**全局令牌桶：200 KiB/s**（约 1.6 Mbps），给网页、上传、QQ 留出余量；下载慢但不会把出口打满。
- 并发下载最多 4 路，满 10 秒返回 503 `busy`；客户端逐场串行下载，不会自己打满。
- 对局索引在内存里缓存，数据有变化时后台重建（约 5 秒，2 vCPU），首个请求会同步建一次。

#### 生产验收（2026-11-06 晚）

```
ping 200 {"version": 2} 0.16s
matches?page_size=3  200  total=8202  total_bytes=6.5MB  5.01s（冷索引；热态 <0.5s）
match.zip            200  2.5KB  files: 1_….yrp / 1_game.json / match.json
单页 50 场 bytes: min 513 / avg 712 / max 999（.yrp 本身很小，主要体积在未下载的证据 JSON）
排名筛选（2026-11-06 15:10 实测）：全部 6,307 场；我方前100 2,065；对手前100 2,060；双方都前100（前100内战）472；指定 top1 玩家 115 场（其自我方前100 仍 115，符合预期）
双侧玩家名（2026-11-06 15:26 实测）：全部 6,321；`player=Ssaumel` 2；`opponent=罗夏hb` 35；两者同时 1；`player=opponent` 同名 0
双侧卡组（2026-11-06 15:34 实测）：全部 6,328；样例场 `灬安小沫丶(幕帘味美喵) vs 梦里的御遇域(混沌太阳神)` 用四个条件同时筛 → 1 且命中该场；`self_deck=幕帘味美喵` 104；`opponent_deck=混沌太阳神` 29
子串匹配（2026-11-06 15:40 实测）：`self_deck=幕帘味美喵` 106、`幕帘` 334、`味美` 127、`耀圣` 483、`不存在的卡组` 0
以对战为单位（2026-11-06 15:48 实测）：`self_deck=幕帘味美喵` 104、`幕帘` 308、`味美` 127、`耀圣` 483、`不存在卡组xyz` 0
```

#### 客户端

MoeCardWatch v1.2「录像库」：一场一行（双方/比分/卡组/局数/大小），勾选多场「下载所选」，
「下载全部（当前筛选）」先给总大小与预计时间，下载中显示进度、速度和剩余时间，可停止。
实现 `clients/MoeCardWatch/app/replay_api_client.py`、`replay_browser.py`。

### 7.13 `GET /replay-api/player?name=<玩家名>`（PC 玩家查询，2026-11-09）

给 PC 客户端「玩家查询」页用：返回萌卡**官方**当前排名/积分与**当月**战绩，口径与 QQ「历史 玩家名」一致。

- 数据源：`https://sapi.moecube.com:444/ygopro/arena/user`（`arena_rank`、`pt`）+ `arena/history`（`type=1`，两页 50 条）；不读本站事实库。
- 缓存：同一玩家 60 秒；服务端单次约 1–3 秒。
- 鉴权：与录像库相同，`Authorization: Bearer wt_...`。

```json
{
  "ok": true, "player": "捏爆你的小旺仔",
  "rank": 427, "score": 1195.0,
  "label": "2026-11", "count": 6, "wins": 5, "losses": 1, "draws": 0,
  "win_rate": 83.33,
  "games": [{"recorded_at": 1793668362.0, "opponent": "liberal", "outcome": "win",
             "before": 1131.91, "after": 1152.91, "delta": 21.0}]
}
```

`rank=0` 表示未上榜；`outcome` 为 `win/loss/draw/unknown`；`before/after/delta` 是该场前后竞技分。
错误：400 `player_required`/`bad_player`；503 `official_unavailable`（萌卡接口超时或不可用）。

## 8. 统计网页录像筛选（`/statistics/api/recordings`）

统计网页「录像筛选」页的查询接口，和上面的 `/query-api`、`/replay-api` 是两套东西：

- 本地 owner 页面：`http://127.0.0.1:8788/statistics/api/recordings?<params>`（需 owner cookie）
- 公开统计链接：`http://<服务器>:8787/stats/<token>/statistics/api/recordings?<params>`（当前由collector的统计代理转发到ygo-web，不在collector重算）

现有参数（节选）：`mode`（drawn/not_drawn/used）、`match`、`card`、`not_card`、`deck`、`series`、`opponent_*`、`self_range` / `opponent_range`（100/200/all）、`start` / `end`（ISO 时间）、`outcome`、`first`、`phase`。

**2026-11-06 新增：`player`** —— 玩家名包含匹配（≤64 字，大小写不敏感），过滤“我方”，即每行录像视角的玩家（`row.player`）。留空=全部玩家。**网页 UI 上的「我方玩家名」输入框已在 18:40 撤掉**（与页面原有风格不搭），参数本身保留，URL/API 仍可直接使用；PC 录像库用的是另一套 `/replay-api` 的 `player`。示例：

```
/statistics/api/recordings?player=%E9%A3%8E&self_range=100&opponent_range=all
```

返回沿用原结构（`total`、`samples`、`metrics`、`rank_scope`），打包下载沿用同一组条件。

**部署状态（2026-11-06 15:45 起）**：`bot/statistics_recording_filters.py` 已在`ygo-web`/`ygo-collector` 加载，8787/8788 都支持 `player`；卡组条件已恢复为子串匹配。18:40 撤掉页面输入框只改 HTML，不需重启服务。

**只读验收（生产库直接调用）**：基线 5,864 个有效视角；`player=风` → 137，且样本全部属于该玩家；不存在的玩家 → 0。

## 9. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-05 | 查询接口候选定稿并部署（`036d2b7`），本文 1～6 节为该版本契约 |
| 2026-11-06 | 新增第 7 节录像库接口 `/replay-api`（只读、设备令牌、按 digest 去重检索、单文件下载）；补记查询接口生产状态 |
| 2026-11-06 晚 | 录像库升级 v2：按 session 聚合“对局”，新增 `/matches`（玩家/卡组/日期）、`/match`、`/match.zip`（整场文件夹 + match.json + 每局 game.json），下载全局限速 200 KiB/s；v1 的 `/search`、`/download` 保留兼容；客户端升到 v1.2 |
| 2026-11-06 15:12 | 录像库 `/matches` 增 `self_rank`/`opponent_rank`（全部/前100，当前榜单）；客户端 v1.3 |
| 2026-11-06 15:20 | 统计网页录像筛选增 `player` 玩家名参数（8788 已生效；8787 公开链接待 collector 重启） |
| 2026-11-06 15:40 | 网页录像筛选的卡组条件恢复为子串匹配（此前被旧基线覆盖成精确匹配） |
| 2026-11-06 15:48 | 录像库卡组筛选改为以对战为单位（不再逐局比较），客户端仍为 v1.5 |
| 2026-11-06 18:40 | 网页录像筛选撤掉「我方玩家名」输入框；`player` 参数保留（PC 端用 `/replay-api` 的同名参数） |
| 2026-11-10 14:20 | `/replay-api` 玩家名去首尾空白归一（修复尾随空格导致同一人被拆成两方/三份卡组/比分 0:0:2） |
| 2026-11-08 22:29 | 卡牌 `metrics.samples` 默认截到最近 200 条 + `samples_truncated`；网页 `full=1` 仍全量（gzip 608 KB → 33 KB） |
| 2026-11-08 22:47 | 卡牌 `metrics` 不再含 `investment`（安卓不读；网页 `full=1`/浏览器 UA 保留）；`card-metrics` 冷算曾 28–48 s，超 query_core 8 s 超时 |
| 2026-11-08 23:20 | `card-metrics` 进程内缓存 600 s；`card_query` 6.4 → 1.8–2.6 s；portal 冷算 10.4 → 6.85 s |


## 10. 绑定、换备与单卡统计（2026-11-12已上线）

本节为7个新增端点的完整契约，和§2同属/query-api。功能版本076ada2；部署、测试与回滚记录见[上线收尾](WRAPUP_QUERY_API_ANDROID_20261112.md)。本节字段为结构示例，数组可省略条目，示例数字不代表同一真实快照。

### 10.1 通用约定

公网入口为 `https://<服务器>:8443/query-api/`，使用 `Authorization: Bearer <query access token>`；自签证书须显式信任，不关闭证书校验。绑定身份只取token中的identity，不接受正文或参数选择其他账号。观战令牌和管理令牌不能代替查询access。

成功包装为 `{"ok":true,"data":{}}`，失败为 `{"ok":false,"error":"错误码"}`；繁忙响应可附retry_after秒数及Retry-After响应头。

| HTTP | error | 含义 |
|---|---|---|
| 400 | bad_player / bad_code / bad_param | 名字、卡号或参数非法；未知/重复参数也拒绝 |
| 400 | player_required | 绑定玩家名缺失或去首尾空白后为空 |
| 401 | unauthorized | 查询access缺失、过期、吊销或令牌用途错误 |
| 404 | no_build | 玩家或指定digest无可用构筑，仅适用于换备 |
| 404 | card_not_found | 本地卡库及既有可信身份映射无法识别该卡号 |
| 429 | busy | 查询闸门或上游繁忙、绑定写入限流；按Retry-After退避 |
| 503 | query_unavailable | 依赖不可用、上游超时或统计尚未能可靠产出；可有限退避重试 |

以上错误约定仅适用于本节新端点；旧端点保留原错误语义，查询关闭等通用状态仍有效。冷查usage/winrate实测曾在约8秒返回503，重试后成功，不能将503理解成“没有使用记录”。已知卡牌无记录正常200；使用相关计数为0，无有效分母的胜率为null。coverage、0张分布、usage.records中未携带该卡的玩家及flow.items中使用量为0的卡组，均按真实统计保留，不一律清空。

| 端点 | 缓存与限制 |
|---|---|
| GET binding | 不缓存 |
| POST binding / binding/unbind | 绑定、改绑、解绑合计每账号10次/分钟；不要求订阅writes权限 |
| side-plan | 查询层30秒缓存，包含玩家及全部筛选参数 |
| usage / flow / winrate | 查询层300秒缓存；区分端点、卡号及适用的limit |

统计端点沿用heavy闸门（默认2路），不占watch闸门。查询层及门户复用现有有界缓存/并发机制；不保证网页、QQ、API跨进程全局单飞。绑定与统计端点均跟随query.enabled，本次没有新增binding_enabled配置。

所有统计响应data含generated_at，表示底层结果生成时间，缓存命中不刷新。时间均为UTC Unix秒，可带小数；计数/卡号为整数。flow.rate为0–1，winrate.rate为0–100；无分母为null，真实零值为0。平局进胜率分母，未知结果不当负局；不同接口不保证同一快照。

新增字段向后兼容，客户端忽略未知字段。JSON为UTF-8；玩家名去首尾空白后最多100个Unicode码点，拒绝控制字符、斜杠和反斜杠。路径段空格编码为%20、字面加号为%2B、百分号为%25，只解码一次，不对路径使用unquote_plus。没有分页的列表不将total_decks等字段解释为可分页总数。

### 10.2 身份绑定

与QQ共用qq_bindings；绑定仅设置默认查询玩家，不证明拥有MC账号、不授予额外权限。GET只返回player，不暴露表内updated_at。

### 10.2.1 `GET /query-api/binding`

响应：

```json
{"ok": true, "data": {"player": "捏爆你的小旺仔"}}
```

未绑定：`{"ok":true,"data":{"player":null}}` —— **不要返回 404**，客户端据此显示"未绑定"。

### 10.2.2 `POST /query-api/binding`

请求：

```json
{"player": "捏爆你的小旺仔"}
```

响应：

```json
{"ok": true, "data": {"player": "捏爆你的小旺仔", "known": true, "changed": true}}
```

字段语义：

| 字段 | 说明 |
|---|---|
| `player` | 实际写入的名字（归一化后的真实名） |
| `known` | `store.has_player(player)`：最近 14 天收录里有没有这个名字 |
| `changed` | 与旧绑定不同才为 true |

校验与处理：

1. player 必须为字符串；缺失或去首尾空白后为空 → `400 player_required`；类型错误、超过100个 Unicode 码点、含控制字符或 `/`、`\` → `400 bad_player`。
2. **归一化**：复用 `Service._canonical_player()` 的语义 —— 在 `store.player_names()` 里做"空格归一后唯一匹配"替换（`久 远` → 真实写法）；不唯一或不命中就按原文保存。
3. `known=false` **仍然保存**（可能最近没打），由客户端提示"最近 14 天没收录到这个名字"。
4. 不需要 `query.writes_enabled`：这是个人绑定，不是订阅/统计写入。绑定、改绑、解绑按账号共用上述限流；本版不新增配置开关。
5. 不进入 QQ 那边的 `_bind_pending` 流程，一次调用成事。

### 10.2.3 `POST /query-api/binding/unbind`

请求无正文。响应：

```json
{"ok": true, "data": {"removed": true}}
```

本来就未绑定 → `removed:false`，**不报错**。

### 10.3 换备（Side Plan）

### 10.3.1 `GET /query-api/player/{name}/side-plan`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `name`（路径） | 字符串 ≤100 字 | 必填 | 玩家名，百分号编码；按 §1 编码与校验 |
| `digest` | 64 位小写 hex | 空 | 指定某套构筑；空=最新一套 |
| `top` | 1–12 | 6 | 取最常遇到的 N 个对手卡组 |
| `recent` | 1–10 | 5 | 每个对手取最近 N 场 |
| `min_side_games` | 1–100 的整数 | 3 | 低于该值标 `sample_low` |
| `opponent_range` | 200 / 0 | 200 | 只看对手前 200；`0`=关闭过滤。**默认必须 200**，否则数字和 QQ/网页对不上 |

响应：

```json
{"ok":true,"data":{
  "version":1,"generated_at":1793940116.0,
  "player":"捏爆你的小旺仔",
  "deck":"刻魔泪冠",
  "selected":{"digest":"…","recorded_at":1793940116.0,"observation_kind":"first"},
  "total_matches":37,
  "top_decks":6,"recent_matches":5,"min_side_games":3,
  "opponents":[
    {
      "deck":"珠泪哀歌",
      "matches":9,
      "recent_matches":5,
      "side_games":6,
      "seat_unknown_games":0,
      "sample_low":false,
      "first":{
        "side_games":3,
        "added":[{"code":14558127,"name":"灰流丽","partition":"main",
                  "sessions":2,"side_games":2,
                  "copies":{"1":2},"min_copies":1,"max_copies":1}],
        "removed":[{"code":23434538,"name":"增殖的G","partition":"main",
                    "sessions":1,"side_games":1,
                    "copies":{"3":1},"min_copies":3,"max_copies":3}]
      },
      "second":{"side_games":3,"added":[],"removed":[]},
      "sessions":[
        {"id":14930,"recorded_at":1793940116.0,"game_numbers":[1,2,3],
         "side_games":2,"added":[],"removed":[]}
      ]
    }
  ],
  "rank_scope":{"opponent_range":200,"excluded_matches":2,"unverified_matches":1},
  "notes":["只统计当前构筑（主卡相同）的场次；每个对手取最近 5 场。","…"]
}}
```

要点：

- `first` / `second` = **先攻 / 后攻**（按该换边局玩家自己的 `first_player` 座位，不是首局座位）。
- `added` / `removed` 必须补 `name`（直接复用统计门户已做的卡名装饰，不在查询层重复补名；局内 sessions 的卡牌项也遵循同样规则）。
- `copies` 是"张数 → 场次"的直方图；`min_copies`/`max_copies` 是区间，客户端显示成 `1~2 张`。
- `404 no_build`：`query()` 返回 None 时（该玩家最近 14 天没有可用构筑）。QQ 现在回中文句子，API 要换成错误码。
- 已有构筑但没有符合条件的对手场次：200、opponents=[]；与 no_build 区分。指定 digest 对该玩家无可用构筑时也返回 no_build。
- opponent_range=0 在适配层转换为既有实现的关闭过滤值（如 None），不可假定直接传整数 0 语义相同。
- 参数严格校验，非法值返回 400 bad_param，不静默截到边界；整数参数不接受小数、布尔值或重复参数。
- 顺序直接沿用原实现（对手排行、sessions、换入换出卡牌），不在安卓专用接口另排一遍。
- 耗时以实际验收为准；复用现有有界上游超时，不为接口随意放宽到30秒以上。

### 10.4 卡牌统计

### 10.4.1 公共约定

- `{code}`：仅十进制整数，范围 1–4294967295；非法 → 400 bad_code。合法但卡库和既有可信映射均不识别 → 404 card_not_found；仅“没有统计记录”不能判为不存在。
- 卡号通过本地精确查询及既有 card_identity 异画/先行号规则确定身份组；不改变映射、合并算法或参数。不能只把 code 转成卡名再走模糊匹配。接口使用精确卡号入口，复用原计算核心；QQ原调用行为保持兼容。
- 返回 requested_code（本次请求整数）、code（既有身份规则实际选用的代表号）、name（该身份中文名）。二者可相同；映射依据必须来自既有可信规则。
- usage 支持 limit=1..100，默认20；winrate 支持 limit=1..20，默认20；flow 不支持 limit，传入返回400 bad_param。limit 必须为整数，非法值返回400 bad_param，不静默截断。
- 每个统计响应 data 含 generated_at。聚合值必须基于完整数据，不能裁剪后重算；不得修改共享缓存字典来裁剪响应。
- 已知卡牌无记录返回正常空统计；服务故障返回503，详见 §10.1。
- 以下 JSON 只展示字段结构与类型，不是同一批真实数据；空数组/空对象及字符串“…”表示示例省略，不代表实际统计值。实际响应必须为标准 JSON，不含 NaN、Infinity 或省略号字面量。

### 10.4.2 `GET /query-api/card/{code}/usage`

复用使用量统计结果，只裁剪 records。响应结构示例：

```json
{"ok":true,"data":{
  "requested_code":14558127,"code":14558127,"name":"灰流丽","generated_at":1793940116.0,
  "covered":100,"current":100,"excluded":0,"total_players":100,
  "main_dist":{"0":2,"1":3,"2":8,"3":87},
  "side_dist":{"0":74,"1":12,"2":9,"3":5},
  "total_dist":{"0":1,"1":2,"2":7,"3":90},
  "main_total":280,"side_total":45,"total_total":325,
  "side_used":26,"side_available":true,
  "records":[
    {"player":"…","rank":1,"main":3,"side":1,"side_out":0,"total":4,
     "main_known":true,"known":true,"build":"…","has_build":true}
  ],
  "limit":20,"records_total":100,"truncated":true
}}
```

records 只取原实现顺序的前 limit 条；records_total 为裁剪前条数，truncated = records_total > len(records)。各分布与总数不受 limit 影响，顺序沿用原实现。

口径提示（写进 `notes` 或字段注释即可，别改算法）：主卡只认**确证第 1 局**；`side` 是从第 2/3 局相对首局的净换入反推，录像本身不含备牌表。

### 10.4.3 `GET /query-api/card/{code}/flow`

响应结构示例（不返回逐玩家 records 和绘图内部 shown）：

```json
{"ok":true,"data":{
  "requested_code":14558127,"code":14558127,"name":"灰流丽","generated_at":1793940116.0,
  "covered":86,"current":100,"total_players":100,
  "main_used":87,"side_used":26,"total_used":90,
  "items":[
    {"deck":"珠泪哀歌","players":12,
     "main":{"known":12,"used":12,"one":1,"two":2,"three":9,"rate":1.0},
     "side":{"known":12,"used":4,"one":1,"two":1,"three":2,"rate":0.333},
     "total":{"known":12,"used":12,"one":1,"two":1,"three":10,"rate":1.0}}
  ],
  "bars":{"main":[],"side":[],"total":[]},
  "small_groups":[{"deck":"…","players":2,"main":{},"side":{},"total":{}}]
}}
```

`items` 为全部卡组、`bars` 仅包含达到现有样本阈值的卡组、`small_groups` 为低样本卡组。第一版三者完整保留，不支持 limit、不独立截断；顺序和阈值均沿用 collect_flow。用真实全量响应测量体积，不承诺 <32 KB。

### 10.4.4 `GET /query-api/card/{code}/winrate`

响应结构示例：

```json
{"ok":true,"data":{
  "requested_code":14558127,"code":14558127,"name":"灰流丽","generated_at":1793940116.0,
  "matchups":{
    "version":1,"unit":"used_small_game_perspective",
    "population":"top100","partition":"main",
    "limit":20,"min_games":10,"fallback_games":5,
    "base":{"games":1240,"wins":660,"losses":540,"draws":40,
            "rate":53.23,"unknown_outcome":0},
    "rows":[{"deck_type":"珠泪哀歌","games":120,"wins":70,"losses":50,
             "rate":58.33,"low_sample":false}],
    "total_decks":18,"qualified_decks":11,
    "notes":["…"]
  },
  "order":{
    "first":{"games":640,"wins":350,"losses":290,"draws":0,"rate":54.69,"unknown_outcome":0},
    "second":{"games":600,"wins":310,"losses":250,"draws":40,"rate":51.67,"unknown_outcome":0},
    "unknown":{"games":0,"wins":0,"losses":0,"draws":0,"rate":null,"unknown_outcome":0}
  }
}}
```

`order.first` / `order.second` 是**先攻/后攻**（现有分组），直接来自 `card_winrate._compute` 里的 `order`，不要自己重算。

`matchups.rows` 对安卓 API 默认最多20项，支持 limit=1..20；样本不足时可更少。matchups.limit 返回本次有效选择上限。保留 total_decks、qualified_decks 原有含义，不将其直接解释成可分页总数。未知座位只进入总体及 order.unknown，不挪入先攻或后攻。

条数只影响对阵列表；总体、先后攻和未知座位统计基于完整样本。API和数据层缓存区分limit；QQ原调用仍默认5。筛选沿用每行至少10局，不足目标条数时用5–9局补位并标low_sample；没有足够样本时可少于20行，不降阈值凑数。API显式limit=5与QQ Top5沿用同一筛选和顺序。

### 10.5 调用示例

下面使用Bash及专用测试账号；令牌只在内存输入，不写入文档或命令历史。TLS证书通过受信证书库或CURL_CA_BUNDLE配置，不使用-k。示例中的玩家名替换为实际名称。

```bash
read -r -s -p 'Query access token: ' TOKEN; echo
B='https://<服务器>:8443'
curl -s -H "Authorization: Bearer $TOKEN" "$B/query-api/binding"
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"player":"示例玩家"}' "$B/query-api/binding"
curl -s -X POST -H "Authorization: Bearer $TOKEN" "$B/query-api/binding/unbind"
NAME=$(python3 -c 'import urllib.parse; print(urllib.parse.quote("示例玩家", safe=""))')
curl -s -H "Authorization: Bearer $TOKEN" "$B/query-api/player/$NAME/side-plan?top=6&recent=5"
curl -s -H "Authorization: Bearer $TOKEN" "$B/query-api/card/14558127/usage?limit=20"
curl -s -H "Authorization: Bearer $TOKEN" "$B/query-api/card/14558127/flow"
curl -s -H "Authorization: Bearer $TOKEN" "$B/query-api/card/14558127/winrate?limit=20"
unset TOKEN
```

内部 `/statistics/api/query-*` 不是公网对接入口；它们要求本机Host/来源、owner cookie及内部头，由服务端查询层调用。
