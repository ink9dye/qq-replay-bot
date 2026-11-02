# 远程观战接口（多 IP / 多账号）

服务器只负责**发现房间、分配任务、去重、入库**；真正的观战连接由"观战端"
（小程序）发起，因此每个观战端可以来自不同公网 IP、用不同萌卡账号。

## 1. 角色

| 角色 | 位置 | 职责 |
|---|---|---|
| 调度端（大程序） | 现有 `ygo-collector` 进程 | 房间发现、选择、节流、派发、录像入库、统计 |
| 观战端（小程序） | 任意有公网 IP 的电脑/服务器 | 认领任务、连萌卡、录制、上传 `.yrp` |

调度端在 `watch.py` 的 `serve()` 里：远程端在线时先派发，派不出去才本地观战
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

## 3. 接口（全部需要 `Authorization: Bearer <token>`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/watch-api/claim` | `{"worker":"pc-a","capacity":2,"ip":"1.2.3.4"}` → 200 `{"job":{...}}` 或 204 |
| POST | `/watch-api/heartbeat` | `{"worker":"pc-a","active":["<job-id>"]}` → `{"ok":true}` |
| POST | `/watch-api/report` | `{"worker","job","outcome","detail"}` → `{"ok":true}` |
| POST | `/watch-api/upload?job=<id>&name=<x.yrp>` | body 为原始字节；`X-Players` 用 URL 编码、`X-Recorded-At` 为时间戳 |
| GET | `/watch-api/status` | 任务板/在线端快照（排障用） |

`job` 结构：

```json
{
  "id": "b31f...", "lease": 120,
  "room": {"key":"M#xxx","title":"...","join_id":"...","players":["甲","乙"],
           "seats":{"0":"甲","1":"乙"},"started":true},
  "server": {"host":"tiramisu.moenext.com","port":8911,"version":5463},
  "password": "<20 字入房口令，服务器用当前账号算好>",
  "nickname": "Spectator",
  "focus": ["甲"],          // 录像归档到哪些玩家目录
  "started_at": 1793635000,
  "reason": "前100第33名"
}
```

> 口令由调度端按**当前 `mycard` 账号**生成。多账号场景下，每个观战端应使用
> 自己的账号：观战端本地跑 `watch.refresh_secret_shared/config` 自行算口令，
> 不依赖下发的 `password`（该字段仅用于单账号过渡）。

## 4. 观战端

```bash
python tools/watch_worker.py --server http://101.201.108.188:8787 \
    --token <remote.token> --name pc-a --capacity 2
```

- 只依赖仓库里的 `watch.py`，标准库即可；
- 临时目录里录制，上传成功即删除；
- 上传 `.yrp` 与 `<name>.yrp.evidence.json`（小局胜负证据）；
- 心跳每 10 秒，任务租期内未 report 会被服务器收回。

## 5. 容量与账号的对应规则（用户口径）

| 可用公网 IPv4 | 观战端/账号 | 行为 |
|---|---|---|
| 1 个 | 0 个 | **保持原样**，全部本地观战 |
| 2 个 | 1 个 | 第二个 IP 上一个观战端/账号 |
| 4 个 | 3 个 | 三个账号分别绑三个 IP（第 4 个留作服务器出口/备用） |

即：观战端数量 = min(可用公网 IP 数 - 1, 账号数)；-1 是留给主服务器的出口。

## 6. 安全注意

- `token` 泄露 = 任何人都能领任务、上传文件（会被限制在 `.yrp`、4MB、玩家目录内）。
- 上传只写到 `data/replays/<focus>/`，文件名做白名单清洗，禁止路径穿越。
- 建议只在需要时开 `remote.enabled`；关掉后接口不启动。
