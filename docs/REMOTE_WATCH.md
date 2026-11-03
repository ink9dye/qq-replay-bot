# 远程观战接口（多 IP / 多账号）

服务器只负责**发现房间、分配任务、去重、入库**；真正的观战连接由"观战端"
（云端 / PC / 安卓）发起，因此每个观战端可以来自不同公网 IP、用不同萌卡账号。

> **安卓端开发指南（推荐先读）**：[ANDROID_CLIENT.md](ANDROID_CLIENT.md)。
> 本文是接口契约与运行规则；安卓的工程结构、Chaquopy/ Kotlin 路线、排障和验收清单在那边。

> 2026-11-03 生产已切成 **server-only**：`watch.local_watch=false`，`ygo-collector`
> 不再自己进房；记录由服务器上的云端客户端 `ygo-cloud-watch` 和后续 PC/安卓客户端完成。
> 一个账号可以**并发进多个房间**（旧 collector 8 路就是同一账号）；但 MyCard 在同账号多房间时
> **可能**拒发回放（取决于平台是否开启 `restrict_multi_room_spectator_replay`）。客户端默认
> `capacity=1` 是保守策略，不是“一个账号不能进多间”的硬限制。

> 2026-11-03 账号本地化：任务里不再下发入房口令，也不再下发服务器账号的昵称。
> 观战端用自己的账号在本机 `watch.sign_in()` 拿 u16Secret，每局进场前用
> `watch.build_join_password()` 算口令。旧版"服务器算好口令、跨机器复用"的做法
> 会因身份/账号不一致被房间拒绝（`主机密码不正确`、`msg=1 code=9`）。

## 1. 角色

| 角色 | 位置 | 职责 |
|---|---|---|
| 调度端（server-only） | 现有 `ygo-collector` 进程 | 房间发现、选择、按 IP 限流、派发、录像入库、统计；**不进房** |
| 云端客户端 | 服务器上的 `ygo-cloud-watch` systemd | 认领任务、用服务器 `mycard` 账号单间录制、上传、上报 |
| 远程客户端（PC/安卓） | 任意有公网 IP 的电脑/手机 | 认领任务、用本机账号单间录制、上传、上报 |

调度端在 `watch.py` 的 `serve()` 里：`local_watch=false` 时只派发；没有客户端接单就
短冷却后重试，绝不本地进房。`local_watch=true` 时保留旧的"派不出再本地观战"行为。

## 2. 开关与配置

`config.json`：

```json
"watch": { "local_watch": false },
"remote": {
  "enabled": true,
  "token": "<随机长串，单独的接口令牌>",
  "bind_host": "127.0.0.1",
  "bind_port": 8791,
  "claim_wait_seconds": 3,
  "lease_seconds": 120,
  "job_ttl_seconds": 180,
  "single_ip_max": 7,
  "multi_ip_max": 6
}
```

- `watch.local_watch=false`：collector 只发现/派发，不进房；缺省 `true` 保持旧行为。
- `enabled=false` 或缺省：完全不启用，行为与旧版一致。
- 接口只监听回环，由 8787 的 nginx 反代：`location /watch-api/ { proxy_pass http://127.0.0.1:8791; }`。
- `token` 是独立令牌，**不要复用** QQ/NapCat/统计的 token。
- `single_ip_max` / `multi_ip_max`：**单个公网 IP** 的并发上限。在线客户端只有 1 个 IP 时
  上限 7；出现第二个 IP 时每个 IP **各自**降到 6（两个 IP 合计最多 12，不是总共 6）。按 claim
  请求的 `X-Real-IP`（云端直连则是 `127.0.0.1`）统计，客户端伪造请求体里的 `ip` 无效。
- `job_ttl_seconds` 只约束**未被认领**的任务；认领后由 `lease_seconds` 续命，心跳在就一直保留
  （长对局不能按 180 秒 TTL 清掉，否则上传后 `report` 会 409）。
- nginx 的 `/watch-api/` 需要 `client_max_body_size` ≥ 录像大小（线上 8m），否则大录像 413。

## 3. 接口（全部需要 `Authorization: Bearer <token>`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/watch-api/claim` | `{"worker":"android-1","capacity":1,"wait":15}` → 200 `{"job":{...}}` 或 204 |
| POST | `/watch-api/heartbeat` | `{"worker":"pc-a","active":["<job-id>"]}` → `{"ok":true}` |
| POST | `/watch-api/report` | `{"worker","job","outcome","detail"}` → `{"ok":true}` |
| POST | `/watch-api/upload?job=<id>&name=<x.yrp>` | body 为原始字节；`X-Players` 用 URL 编码、`X-Recorded-At` 为时间戳 |
| GET | `/watch-api/status` | 任务板/在线端快照（排障用） |

`job` 结构（**没有口令、没有账号昵称**）：

```json
{
  "id": "b31f...", "lease": 120,
  "room": {"key":"M#xxx","title":"...","join_id":"...","players":["甲","乙"],
           "seats":{"0":"甲","1":"乙"},"started":true},
  "server": {"host":"tiramisu.moenext.com","port":8911,"version":5463},
  "focus": ["甲"],          // 录像归档到哪些玩家目录
  "started_at": 1793635000,
  "reason": "前100第33名"
}
```

> `room` 只用于连房间和归档；`focus` 决定上传后落在 `data/replays/<focus>/`。
> 入房口令与账号凭据都不在任务 JSON 里，抓包/日志拿到任务也进不了房间。

## 4. 观战端

```bash
python tools/watch_worker.py --config tools/worker.json
```

`tools/worker.json`（**已 gitignore，只存本机**）：

```json
{
  "server": "http://101.201.108.188:8787",
  "token": "<remote.token，不要复用其它 token>",
  "name": "pc-a",
  "capacity": 1,
  "account": "<本机萌卡账号>",
  "password": "<本机萌卡密码>",
  "work_dir": ""
}
```

- 只依赖仓库里的 `watch.py`，标准库即可；命令行 `--token/--server/--name/--capacity`
  仍可覆盖 worker.json。
- 启动顺序：先 `watch.sign_in(account, password)` 拿 u16Secret；登录失败直接退出，
  **不会空领任务**；token 无效（401）或服务端没开远程（404）也会明确报错。
- 每局进场（含重连）由 `Spectator.run()` 调 `watch.refresh_secret_shared()` 刷新
  u16Secret（60 秒节流），再用 `watch.build_join_password()` 按本局 `join_id` 算口令；
  口令绝不写入日志。
- 账号密码只在 worker.json 和进程内存里；**不打印、不上传、不提交**。
- 临时目录里录制，上传成功即删除；上传 `.yrp` 与 `<name>.yrp.evidence.json`（小局胜负证据）。
- 心跳每 10 秒，任务租期内未 report 会被服务器收回；长轮询 `wait` 用 15 秒（小于 HTTP 20 秒超时）。
- **房间去重**：服务器端已加 15 分钟房间记忆；客户端 `room.key` 去重仍保留兜底。
- **客户端进场节流**：服务器派发绕过本地 `JoinGate`（不占 `in_flight`、不更新 20 秒间隔），
  worker 自己补：两次新连接至少隔 20 秒 + 0–5 秒抖动，避免几秒内连进多间被平台判异常。
- **同账号可以并发进多间，但回放可能被平台限制**：MyCard 的 `mycard-spectator-replay` 在
  `restrict_multi_room_spectator_replay` 开启时，会对 `blockedByMultipleRooms` 拒发回放
  （`CLIENT_send_replays()` 返回 false）；**这不阻止进房**，只影响回放包。`capacity` 是
  **进程认领上限**，可以 >1；当前客户端默认 `capacity=1` 是保守策略。多间是否仍有回放必须按
  平台当前配置实测，不能一概而论；2026-11-03 单间实测能收到 3 个小局回放并入库。

> 2026-11-03 10:13 生产云端/PC 均已改成 `capacity=6`（6+6 并发，两个 IP 各自上限 6）；
> 多间回放仍在观察。新客户端建议先用 1 验证，再按账号/平台实测放大。

## 4.1 云端客户端（ygo-cloud-watch）

服务器上独立 systemd，使用 `config.json` 的 `mycard` 账号，只看一间：

- 配置 `/opt/ygobot/tools/worker-cloud.json`（600，ygo:ygo）：`server=http://127.0.0.1:8791`、
  `name=cloud`、`capacity=1`、`account/password` 来自 `mycard`。
- 单元文件 `deploy/ygo-cloud-watch.service`：`User=ygo`、`Restart=always`、
  `After=ygo-collector.service`；collector 重启后它会自动重连认领。
- 它通过本机 8791 上传，collector 的 ingest 队列落 `data/replays/<focus>/` 并入库。
- 部署：`cp deploy/ygo-cloud-watch.service /etc/systemd/system/` → `systemctl daemon-reload`
  → `systemctl enable --now ygo-cloud-watch`。
- 2026-11-03 09:45 实测：cloud 进 1 间房（人杰地灵 vs DE恶魔），收到 **2 个小局回放**，
  上传 2 个，`replays` +2、`stat_games` +2、`stat_views` +4，`/watch-api/status` 显示
  `ips=["127.0.0.1"]`、`ip_cap=7`。
- collector 重启后任务板为空，但客户端心跳里的 `active` 仍会占用自己的 capacity，
  避免服务器误判空闲而空派发；旧任务跑完后 `report` 可能 409，但已录文件仍会走
  `/watch-api/upload` 入库，客户端随后自动认领新任务。

## 5. 容量与账号的对应规则（用户口径）

| 可用公网 IPv4 | 观战端/账号 | 行为 |
|---|---|---|
| 1 个 | 0 个远程 | **云端客户端 1 间**（server-only；collector 不再本地多路） |
| 2 个 | 1 个 | 第二个 IP 上一个观战端/账号 |
| 4 个 | 3 个 | 三个账号分别绑三个 IP（第 4 个留作服务器出口/备用） |

即：观战端数量 = min(可用公网 IP 数 - 1, 账号数)；-1 是留给主服务器的出口。

> **2026-11-03 生产已 server-only**：collector 只发现/派发，云端 `ygo-cloud-watch` +
> PC/安卓客户端各看一间（默认）。一个账号可以并发进多间；但平台开启多房间回放限制时，
> 回放会被拒。所以“观战端数量”不等于账号数：可以一账号 `capacity>1`，也可以多账号分摊；
> 要稳拿回放先用单间/单进程实测。**单个公网 IP 的并发上限**：只有 1 个 IP 时 7；出现多个 IP 时
> **每个 IP 各自 6**（两个 IP 合计最多 12，不是总共 6），由任务板按 claim 请求 IP
> （`X-Real-IP`；云端直连为 `127.0.0.1`）强制。

多账号 = 每台机器/每个 `worker.json` 写自己的 `account/password`；一个进程一个账号，但
`capacity` 可 >1（同账号多间）；同账号多端还可能互相顶 u16Secret，并触发上面的回放限制。

## 6. 安全注意

- `token` 泄露 = 任何人都能领任务、上传文件（会被限制在 `.yrp`、4MB、玩家目录内）。
- 账号密码泄露 = 别人能登录你的萌卡账号；worker.json 不要发人、不要进仓库。
- 上传只写到 `data/replays/<focus>/`，文件名做白名单清洗，禁止路径穿越。
- 建议只在需要时开 `remote.enabled`；关掉后接口不启动。
