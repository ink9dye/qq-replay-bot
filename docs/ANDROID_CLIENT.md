# 安卓观战客户端开发指南

本文给要开发安卓观战端的人看。**服务端已经写好并在线，安卓端只需要实现“领任务 → 进房 →
录制 → 上传 → 上报”这一条链路**，不需要改服务器。

先读接口契约：[REMOTE_WATCH.md](REMOTE_WATCH.md)。本文补充工程落地、安卓注意事项和验收清单。
令牌领取、客户端名称绑定和排障见 [WATCH_DEVICE_TOKENS.md](WATCH_DEVICE_TOKENS.md)。

## 0. 一页纸：你只需要做这些

1. 在群里/私聊机器人发 **令牌分发**，它会自动回复服务器地址、接口令牌和客户端名称；把客户端名称**原样**填进 APP。令牌只显示一次。也可以向服务器管理员要旧版 `remote.token`（安全渠道）。
2. clone/fork 仓库，读 `docs/REMOTE_WATCH.md` + 本文；参考 `tools/watch_worker.py`。
3. 做一个前台服务：`claim` → 本机 `sign_in`/算口令 → 进房 → 存 `.yrp` → `upload` → `report`；
   心跳每 10 秒。
4. 先在 PC 上用 `tools/watch_worker.py` 跑通同一个账号，再装 APK。
5. 把 **唯一的 worker 名**（如 `android-<你的名字>`）和测试时间发给管理员，让他确认
   `/watch-api/status`、`data/replays/<focus>/` 和统计库入库。
6. 交付 APK/源码 + 一局完整测试日志；日志/源码里不能有 token、账号密码。

> 服务端已经部署好，你**不需要**动 `/opt/ygobot`、不需要重启 collector、不需要数据库权限。

## 1. 目标与边界

安卓端是一个**远程观战客户端**：

- 服务器 `ygo-collector` 负责发现房间、分配任务、入库；它**不进房**。
- 安卓端用自己的萌卡账号登录、自己算入房口令、自己连游戏服录制 `.yrp`，再上传回服务器。
- 服务器上的 `ygo-cloud-watch` 和 PC 端 `pc-b` 是同类客户端，可以拿它们当参考实现。

**一个客户端进程可以认领多间（`capacity>1`），一个账号也可以并发进多间。** 但 MyCard 在同账号
多房间时**可能**拒发回放（取决于平台是否开启 `restrict_multi_room_spectator_replay`）；
所以客户端默认 `capacity=1` 是保守策略：先保证单间回放，多间是否可用要实测。要稳拿 N 间，
可以 N 个进程/N 个账号，也可以一个进程 `capacity=N`（回放是否下发以平台为准）。
2026-11-03 生产云端/PC 现为 `capacity=6`；安卓可先 1 验证，再按账号/平台实测放大。

## 2. 需要准备什么

| 项 | 说明 |
|---|---|
| 服务器地址 | `http://101.201.108.188:8787`（公网入口，nginx 反代到 8791） |
| 接口令牌 | 保密名单内群/私聊发“令牌分发”，机器人自动签发并绑定客户端名称（默认 30 天）；或管理员给的 `remote.token` |
| 萌卡账号 | 你自己的账号，账号密码只存手机本地（Android Keystore/私有存储） |
| 公网 IP | 手机所在网络的公网 IP；服务器按这个 IP 限并发（单 IP 7，多 IP 时每个 IP 各自 6） |
| 参考代码 | `watch.py`（协议/录像核心）、`tools/watch_worker.py`（完整客户端流程） |

> **客户端名称必须和机器人回复完全一致**（例如 `android-123456789`）。每设备令牌绑定这个
> `worker`，名字不一致会返回 401/403，APP 显示“令牌无效或已过期”。换设备再发一次
> “令牌分发 2”。旧版 `remote.token` 仍可用，不受影响。

> `worker` 名必须唯一（如 `android-pixel8`）。服务器用 worker 名区分客户端；两个设备重名会
> 互相覆盖 `active`/容量，表现为“领不到任务”或统计异常。

> 不要把服务器 `config.json` 里的 `mycard` 账号给安卓端用；云端客户端已经占了它。
> 同账号多客户端/多间**可能**被 MyCard 拒发回放（取决于平台限制），且 u16Secret 会互顶；
> 要稳拿回放先用单间，或多账号分摊。

## 3. 两条实现路线

### 路线 A（推荐）：Chaquopy 复用 `watch.py`

`watch.py` 是纯标准库，已经实现了：

- `watch.sign_in(account, password)` → `(u16Secret, username)`
- `watch.refresh_secret_shared(config)` → 60 秒节流刷新 u16Secret
- `watch.build_join_password(config, room)` → 按本局 `join_id` 算 20 字入房口令
- `watch.Spectator(config, room, writer)` → 连服、进房、收包、保存 `.yrp`
- `watch.ReplayWriter(dir, focus, "exact")` → 录像落盘、`on_saved` 回调
- `watch.replay_files_from_payload(body)` → 一个 STOC_REPLAY 包可能含多局录像

把 `watch.py`、`retention_policy.py`、`bot/join_throttle.py` 一起打进 APK（`bot` 需要
`__init__.py`），Python 版本建议 ≥ 3.10（worker 用了 `X | None` 语法）。然后照
`tools/watch_worker.py` 的 `run_job()` 写一个 Android 前台服务即可。

### 路线 B：Kotlin/Java 重写协议

只照 HTTP 接口 + ygopro TCP 帧重写也可以，但**入房口令算法不要自己猜**：直接对照
`watch.py` 的 `encode_mycard_password()` / `build_join_password()`。核心帧：

```text
帧 = uint16le(长度=1+body) + uint8(proto) + body
CTOS_PLAYER_INFO (0x10): body = utf16le(昵称, 固定 20 字/40 字节)
CTOS_JOIN_GAME  (0x12): body = uint16le(version) + uint16le(0) + uint32le(0)
                                + utf16le(入房口令, 固定 20 字/40 字节)
STOC_REPLAY     (0x17): body 就是 .yrp 文件（可能粘多局，需按 magic 切分）
```

连接后先发 `PLAYER_INFO`，再发 `JOIN_GAME`；之后等 `STOC_REPLAY` 保存，`STOC_DUEL_END`
表示本局结束。长连接期间要处理重连、0 字节被关、房间已结束等情况（`watch.Spectator.run()`
已经处理，建议直接复用）。

## 4. HTTP 接口（全部带 Bearer token）

```http
Authorization: Bearer <WATCH_TOKEN>
Content-Type: application/json
```

### 4.1 领任务

```http
POST /watch-api/claim
{"worker":"android-1","capacity":1,"wait":15}
```

- `wait=15` 是长轮询秒数；HTTP 客户端读超时设 **25 秒**（要大于 15）。
- 返回 `200 {"ok":true,"job":{...}}` 或 `204`（没任务，继续轮询）。
- 不要传请求体里的 `ip`；服务器按请求来源 IP 统计，伪造无效。

### 4.2 心跳

```http
POST /watch-api/heartbeat
{"worker":"android-1","active":["<job-id>"]}
```

- 每 10 秒一次；`active` 放当前正在录的 job id。
- **没有任务时不要发 `"active":[]`**：省略 `active` 字段（服务端会把它当“未知/不清理”，
  避免刚认领的任务被误清）。参考 `tools/watch_worker.py` 的 `heartbeat()`。

### 4.3 上报

```http
POST /watch-api/report
{"worker":"android-1","job":"<job-id>","outcome":"saved","detail":{"saved":1,"uploaded":1,"bytes":767}}
```

- `outcome`：`saved` / `noreplay` / `failed`（自定义字符串服务端会记录）。
- 房间结束、上传完成后调用。
- 如果 collector 重启导致任务板重置，这里可能返回 **409**；不影响已经上传的文件入库，
  客户端继续领下一个任务即可。

### 4.4 上传录像

```http
POST /watch-api/upload?job=<job-id>&name=<文件名>.yrp
Body: 原始 .yrp 字节
X-Players: <URL 编码的 "甲,乙">
X-Recorded-At: <epoch 秒，可带小数>
```

- 单文件上限 **4MB**；nginx 上限 8m；超过会 413。
- 上传成功即完成；临时文件可以删。
- 小局胜负证据可选：再上传一份 `<name>.yrp.evidence.json`，`name` 以
  `.yrp.evidence.json` 结尾即可。
- 文件最终落在服务器 `data/replays/<focus>/`，由 collector 的 ingest 队列入库。

### 4.5 状态（排障）

```http
GET /watch-api/status
```

返回 `workers`、`pending`、`claimed`、`ips`、`ip_cap`。安卓端可以打日志，但不要把
token/账号写进日志。

## 5. 客户端主循环（伪代码）

```text
load account/password from Keystore (never log)
verify: GET /watch-api/status
start heartbeat thread (every 10s; omit active when empty)

loop:
  job = POST /watch-api/claim {worker, capacity:1, wait:15}
  if 204: sleep 1s; continue
  if 401/404/502: show error + backoff; continue

  sign_in(account, password) -> secret, username   # 失败则退出，不要空领任务
  room = job.room; config = host/port/version + account/password + secret
  password = build_join_password(config, room)     # 每局/重连都重算
  connect TLS/TCP host:port
  send PLAYER_INFO(username); send JOIN_GAME(version, password)
  read frames; on STOC_REPLAY:
      for each .yrp in replay_files_from_payload(body):
          upload(job.id, file, players, recorded_at)
  on end: report(job.id, "saved"/"noreplay", detail)
```

**账号/口令刷新**：u16Secret 大约每 4 分钟轮换；进场前用 `refresh_secret_shared(config)`
（60 秒节流），重连也要重算口令。`watch.Spectator.run()` 已处理这些细节，优先复用。

**去重**：服务器有 15 分钟房间记忆，但客户端仍应按 `room.key` 去重，避免同一间重复录。
**进场节流**：两次新连接至少隔 20 秒 + 0–5 秒抖动，避免平台判异常。

## 6. 安卓工程注意事项

| 点 | 建议 |
|---|---|
| 权限 | `INTERNET`、`FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_DATA_SYNC`（Android 14+）、`WAKE_LOCK`、`POST_NOTIFICATIONS`（13+） |
| 前台服务 | 用 `ForegroundService`，常驻通知；否则 Doze 会杀长轮询/连接 |
| 网络 | `usesCleartextTraffic=true` 或 network-security-config 只放行 `101.201.108.188:8787`（当前是 HTTP，无 TLS） |
| 超时 | claim 读超时 25s；upload 60s；连接/读包参考 `watch.py` 的 10s/120s |
| 唤醒 | 录制期间 `PARTIAL_WAKE_LOCK`；申请忽略电池优化 |
| 断线 | 指数退避重连；换网/切后台时重建连接；服务器任务租期 120s，心跳别停 |
| 存储 | 只写 App 私有目录；上传成功即删；单文件 ≤4MB |
| 凭据 | Android Keystore/EncryptedSharedPreferences；不写日志、不截图、不进 git |
| 并发 | 一个进程 `capacity` 可 >1，一个账号也可并发多间；但同账号多间可能无回放，稳拿回放先 1 间/1 进程，或实测后再放大 |
| 安全 | 当前 token 是共享令牌；泄露就联系管理员轮换，并同步云端/PC 客户端 |

## 7. 排障表

| 现象 | 原因/处理 |
|---|---|
| 401 | token 不对/被轮换；找管理员要新 token |
| 404 或 502 | 服务端没开 remote 或 collector 没起；找管理员 |
| 204 | 当前没房/客户端已满；继续轮询 |
| 409（report） | 任务过期/collector 重启；文件通常已上传，继续下一个任务 |
| 413 | 录像 >4MB 或 nginx 限制；检查文件 |
| 429 | nginx 限流；退避，别高频打接口 |
| 进房 `主机密码不正确` / `msg=1 code=9` | 账号/昵称/口令不匹配；确认本地 `sign_in` 成功、用本局 `join_id` 重算口令 |
| 进房 `房间拒绝观战` | 房主关了观战；换下一间 |
| 收到 `noreplay` | 平台没下发回放；常见于同账号多房间或房间异常，不是接口失败 |
| 上传成功但页面没数据 | 入库有延迟；让管理员查 `data/replays/<focus>/` 和 `replays` 表 |

## 8. 验收清单

在 PC 上先用参考客户端跑通，再上安卓：

```bash
python tools/watch_worker.py --config tools/worker-android.json --name android-test --capacity 1
```

- [ ] `/watch-api/status` 能看到 `android-test`，`ip` 是你的公网 IP
- [ ] 日志出现 `认领 ...`、`已进入观战，收到小局开局信息`
- [ ] 结束时 `[worker] 结束 ...：saved，上传 N 个`（或 `noreplay`）
- [ ] 服务器 `journalctl -u ygo-collector` 出现 `已派发远程观战 ... → android-test`
- [ ] 服务器 `data/replays/<focus>/` 出现新 `.yrp`
- [ ] 管理员确认 `replays` / `stat_games` / `stat_views` 入库

安卓端上线后至少验证一次完整对局，且日志里没有 token/账号密码。

## 9. 文件地图

| 文件 | 用途 |
|---|---|
| `docs/REMOTE_WATCH.md` | 接口契约、任务 JSON、配置、安全 |
| `docs/ANDROID_CLIENT.md` | 本文：安卓落地、流程、排障、验收 |
| `watch.py` | 协议、入房口令、`Spectator`、`ReplayWriter` |
| `tools/watch_worker.py` | 完整参考客户端（认领/心跳/录制/上传/上报） |
| `tools/worker.example.json` | 客户端配置模板 |
| `deploy/ygo-cloud-watch.service` | 服务器云端客户端单元（安卓不需要部署这个） |
| `tests/test_watch_jobs.py` | 任务板/并发上限的测试，可看边界行为 |

## 10. 交付与回传

完成后给服务器管理员：

- APK（或源码仓库链接）；
- **唯一的 worker 名**（如 `android-pixel8`）和一局完整测试的时间；
- 脱敏后的客户端日志（证明 `claim`/进房/`upload`/`report`）；
- 不要回传 token、萌卡账号密码、`worker-*.json`。

管理员侧确认：

- `/watch-api/status` 里出现该 worker，`ip` 正确、`active` 归零；
- `journalctl -u ygo-collector` 有 `已派发远程观战 ... → <worker>`；
- `data/replays/<focus>/` 有新 `.yrp`，`replays`/`stat_games`/`stat_views` 入库。


有问题先看 `REMOTE_WATCH.md` 的接口和本文第 7 节；仍不通就把**脱敏后的**日志、worker 名、
`/watch-api/status` 输出发给服务器管理员，不要发 token/账号密码。
