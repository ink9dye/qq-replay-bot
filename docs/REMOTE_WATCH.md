# 远程观战接口（多 IP / 多账号）

服务器只负责**发现房间、分配任务、去重、入库**；真正的观战连接由"观战端"
（小程序 / PC）发起，因此每个观战端可以来自不同公网 IP、用不同萌卡账号。

> 2026-11-03 账号本地化：任务里不再下发入房口令，也不再下发服务器账号的昵称。
> 观战端用自己的账号在本机 `watch.sign_in()` 拿 u16Secret，每局进场前用
> `watch.build_join_password()` 算口令。旧版"服务器算好口令、跨机器复用"的做法
> 会因身份/账号不一致被房间拒绝（`主机密码不正确`、`msg=1 code=9`）。

## 1. 角色

| 角色 | 位置 | 职责 |
|---|---|---|
| 调度端（大程序） | 现有 `ygo-collector` 进程 | 房间发现、选择、节流、派发、录像入库、统计 |
| 观战端（小程序） | 任意有公网 IP 的电脑/服务器 | 认领任务、用本机账号登录萌卡、连房间、录制、上传 `.yrp` |

调度端在 `watch.py` 的 `serve()` 里：远程端在线时先派发，派不出才本地观战
（**没有观战端时行为与现在完全一致**）。

## 2. 开关与配置

`config.json`：

```json
"remote": {
  "enabled": true,
  "token": "<随机长串，单独的接口令牌>",
  "bind_host": "127.0.0.1",
  "bind_port": 8791,
  "claim_wait_seconds": 3,
  "lease_seconds": 120,
  "job_ttl_seconds": 180
}
```

- `enabled=false` 或缺省：完全不启用，行为与旧版一致。
- 接口只监听回环，由 8787 的 nginx 反代：`location /watch-api/ { proxy_pass http://127.0.0.1:8791; }`。
- `token` 是独立令牌，**不要复用** QQ/NapCat/统计的 token。
- `job_ttl_seconds` 只约束**未被认领**的任务；认领后由 `lease_seconds` 续命，心跳在就一直保留
  （长对局不能按 180 秒 TTL 清掉，否则上传后 `report` 会 409）。
- nginx 的 `/watch-api/` 需要 `client_max_body_size` ≥ 录像大小（线上 8m），否则大录像 413。

## 3. 接口（全部需要 `Authorization: Bearer <token>`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/watch-api/claim` | `{"worker":"pc-a","capacity":2,"ip":"1.2.3.4"}` → 200 `{"job":{...}}` 或 204 |
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
  "capacity": 6,
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
- **客户端房间去重**：服务器派发没有"已派发"去重，capacity>1 时同一 `room.key` 可能被连续派两次；
  worker 按房间同时只录一份，且 30 分钟内不重复录，重复任务直接回 `noreplay + duplicate`。

## 5. 容量与账号的对应规则（用户口径）

| 可用公网 IPv4 | 观战端/账号 | 行为 |
|---|---|---|
| 1 个 | 0 个 | **保持原样**，全部本地观战 |
| 2 个 | 1 个 | 第二个 IP 上一个观战端/账号 |
| 4 个 | 3 个 | 三个账号分别绑三个 IP（第 4 个留作服务器出口/备用） |

即：观战端数量 = min(可用公网 IP 数 - 1, 账号数)；-1 是留给主服务器的出口。

多账号 = 每台机器/每个 `worker.json` 写自己的 `account/password`；一个进程只用一个
账号，避免同账号多端同时刷新 u16Secret 互相顶掉。

## 6. 安全注意

- `token` 泄露 = 任何人都能领任务、上传文件（会被限制在 `.yrp`、4MB、玩家目录内）。
- 账号密码泄露 = 别人能登录你的萌卡账号；worker.json 不要发人、不要进仓库。
- 上传只写到 `data/replays/<focus>/`，文件名做白名单清洗，禁止路径穿越。
- 建议只在需要时开 `remote.enabled`；关掉后接口不启动。
