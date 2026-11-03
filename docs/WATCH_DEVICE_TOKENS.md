# 每设备观战接口令牌（QQ 命令：令牌分发）

本文是 2026-11-03 上线的“每设备观战令牌”功能的完整说明，面向三类读者：

- 群友/安卓用户：怎么领令牌、怎么填 APP；
- 管理员：怎么控制保密名单、查看和吊销令牌；
- 维护者：服务端如何鉴权、数据表、部署/回滚和排障。

> 结论：`remote.token` 仍是兼容总令牌；QQ 机器人新增 `令牌分发`，为保密名单内的设备签发绑定 `worker` 的独立令牌。数据库只存 SHA-256，令牌默认 30 天，可重发、过期、吊销。

## 1. 一句话流程

```text
保密名单内的群/私聊 -> 发送“令牌分发”
  -> 机器人回复：服务器地址 + wt_... 接口令牌 + 客户端名称 + 有效期
  -> APP 填这三项，客户端名称必须与机器人回复完全一致
  -> collector 校验 Bearer：总令牌或 watch_tokens 中的每设备令牌
```

## 2. 用户怎么用

### 2.1 领取

在允许的群里直接发（固定格式可免 @，以线上配置为准）：

```text
令牌分发
```

私聊机器人也可以。机器人的回复格式：

```text
观战接口令牌（已绑定客户端）
服务器地址：http://101.201.108.188:8787
接口令牌：wt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
客户端名称：android-123456789
有效期至：2026-12-03 21:40
客户端名称必须原样填；到期或泄露后重新发送“令牌分发”换新。
```

- 接口令牌只显示这一次；数据库不保存明文，丢了只能重发。
- `客户端名称` 由 QQ 号生成：`android-<QQ号>`；第二个设备发 `令牌分发 2`，得到 `android-<QQ号>-2`；槽位 1–9，槽位 1 可省略。
- 重发同一个槽位会**吊销该客户端名称的旧令牌**；旧令牌立即 401。
- 30 秒内重复发送会提示“令牌刚发过”，避免刷屏和误吊销。

### 2.2 填进 MoeCardWatch 安卓端

| APP 字段 | 填什么 |
|---|---|
| 服务器地址 | 机器人回复的地址，当前生产为 `http://101.201.108.188:8787` |
| 接口令牌 | 机器人回复的 `wt_...`，不要带引号/空格 |
| 客户端名称 | 机器人回复的 `android-<QQ号>`，**必须原样**，不要用默认 `android-1` |
| 萌卡账号/密码 | 用你自己的账号；不要用服务器/云端账号 |

服务器地址默认就是生产入口；如果 APP 里已预填，确认端口是 `8787`。  
客户端名称不一致时，服务端返回 401/403，APP 通常显示“令牌无效或已过期”，但真正原因往往是名称填错。

### 2.3 旧版总令牌

PC/云端客户端仍可使用 `config.json` 的 `remote.token`；它不绑定 `worker`，行为与上线前一致。  
安卓用户优先用 `令牌分发`，不要向管理员索要总令牌。

## 3. 命令与权限

### 3.1 命令表

| 命令 | 位置 | 作用 |
|---|---|---|
| `令牌分发` | 群/私聊 | 签发/重发第 1 个设备令牌 |
| `令牌分发 2` … `9` | 群/私聊 | 第 2–9 个设备令牌 |
| `令牌分发 帮助` | 群/私聊 | 用法 |
| `令牌分发 列表` | 仅管理员私聊 | 查看有效令牌（只显示 hint，不显示完整令牌） |
| `令牌分发 吊销 <客户端名称>` | 仅管理员私聊 | 吊销该 worker 的有效令牌 |
| `令牌分发 吊销 QQ <QQ号>` | 仅管理员私聊 | 吊销该 QQ 的所有有效令牌 |

兼容别名：`观战令牌`、`接口令牌`、`端口令牌`，实际以 `令牌分发` 为主。

### 3.2 “保密名单”判定

签发入口在 `bot/watch_token_command.py` 的 `_allowed()`：

1. 群聊：取 `remote.token_groups`；**只要配置里存在这个键**（包括空数组），就只用它，不再看统计名单；键不存在时回退 `statistics.allowed_groups`。群号必须在列表内。管理员身份**不能**绕过群名单。
2. 私聊：取 `remote.token_users`；存在该键时只用它；否则回退 `statistics.private_users`。名单内用户或管理员可以签发。
3. 以上之前仍走机器人原有的 `authorized()`：`qq.allow_all_users=true`、`qq.users`、`qq.admins`。如果用户连机器人普通命令都没有权限，会先收到“你没有使用权限”，不会到令牌逻辑。

线上当前：

- `statistics.allowed_groups` 有 2 个群；
- `statistics.private_users` 有 3 个用户；
- `qq.admins` 1 人；
- `remote.token_groups` / `remote.token_users` 未配置，因此按统计名单执行。

新增群/用户时：

```json
"remote": {
  "token_groups": ["<群号>"],
  "token_users": ["<QQ号>"]
}
```

只改这两个键不需要重启 collector，重启 `ygo-qq` 即可生效。  
若想彻底关闭群签发但保留私聊，可设 `"token_groups": []`；想关闭私聊，设 `"token_users": []`（管理员仍可签发和管理）。

## 4. 令牌生命周期

### 4.1 生成与存储

- 明文：`wt_` + `secrets.token_urlsafe(32)`，共 46 个字符，URL-safe，适合 HTTP Bearer 和 APP 输入框。
- 数据库只存 `SHA-256(token)`，另有 `token_hint`（前 7 + 后 4，供管理员列表识别）。
- 默认有效期：`remote.device_token_hours`，未配置为 720 小时（30 天）；`0` 按 10 年处理。
- 同一 `worker` 只保留一个有效令牌；重发会把旧行标为 `revoked_at`、`revoked_by='reissue'`，然后插入新行。
- 过期/吊销的行保留用于审计，不物理删除；管理列表只显示 `revoked_at IS NULL AND expires_at > now` 的行。

表结构（`data/bot.sqlite3`）：

```sql
CREATE TABLE watch_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    token_hint TEXT NOT NULL,
    worker TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL,
    revoked_by TEXT NOT NULL DEFAULT ''
);
```

`bot/store.py` 的 collector 迁移会建表；`WatchTokenStore(migrate=True)` 也会兜底建表。  
表是**新增**的，不修改旧表，不需要 schema 版本升级。

### 4.2 过期与吊销

- 过期：`expires_at <= now` -> `verify()` 返回 `None` -> 401。
- 吊销：管理命令把 `revoked_at` 置为当前时间 -> 下一次请求 401；collector 每次请求都查库，无需重启。
- 重发：同一 worker 的旧行被吊销；其他槽位/其他 QQ 不受影响。
- 管理员按 QQ 吊销：该 QQ 的全部有效令牌（含槽位 2–9）一起失效。

### 4.3 明文令牌的边界

- 明文只出现在 QQ 回复消息中；代码不写日志、不落盘。
- `watch_tokens`、备份、`token_hint` 都不能还原完整令牌。
- 群里发令牌意味着**群成员都能看到并复制**；这是“小群自动发布”的既定取舍。
- 令牌是 Bearer，不是设备证书；拿到的人可以在别处使用，但只能使用绑定的 `worker`。
- 怀疑泄露：重发同一槽位，或管理员吊销；不要只改 APP 里的名称。

## 5. 服务端鉴权（与安卓 APK 的兼容性）

### 5.1 APK 实际请求

对 `MoeCardWatch-Android-v1.0.0` 的静态核对：

- 包名 `com.moecard.watch`；
- 所有 `/watch-api/*` 请求带 `Authorization: Bearer <接口令牌>`；
- `User-Agent: MoeCardWatch/1.0.0`；
- 使用：`GET /watch-api/status`、`POST /watch-api/claim|heartbeat|report`、`POST /watch-api/upload?job=...&name=...`；
- 令牌字段为空时必须手填；默认客户端名是 `android-1`，必须改成机器人给的名称。

### 5.2 collector 的校验顺序

`bot/watch_api.py`：

1. 先比对 `remote.token`（兼容总令牌）；命中则 `auth_worker=""`，不限制 worker。
2. 否则查 `watch_tokens` 的 SHA-256；命中且未过期/未吊销，记录该行的 `worker`。
3. 都不命中 -> 401。

### 5.3 worker 绑定与状态码

| 请求 | 每设备令牌的规则 | 失败 |
|---|---|---|
| `GET /status` | 任意有效令牌都可查看；不校验 worker | 401 |
| `POST /claim` | 请求体 `worker` 必须等于令牌绑定值 | 403 `worker_mismatch` |
| `POST /heartbeat` | 同上 | 403 |
| `POST /report` | 同上 | 403 |
| `POST /upload` | 请求里没有 worker；按 job 的 `claimed_by` 校验；collector 重启后任务板为空时允许继续上传旧任务 | 403 |

- 401：无令牌、令牌未知、已过期、已吊销。
- 403：令牌有效但客户端名称不匹配。
- 204：claim 没有任务，正常长轮询结果，不是错误。
- APP 对 401/403 都显示“令牌无效或已过期”；排障时先核对客户端名称。

### 5.4 旧客户端兼容

- `remote.token` 继续有效，PC/云端不需要改配置。
- 上线后实测旧令牌 `status 200`，线上 8 个 worker 记录、7 个在线。
- 只有显式使用每设备令牌的客户端才受 worker 绑定限制。

## 6. 配置

```json
"remote": {
  "enabled": true,
  "token": "<兼容总令牌，保留>",
  "public_url": "http://101.201.108.188:8787",
  "device_token_hours": 720,
  "bind_host": "127.0.0.1",
  "bind_port": 8791
}
```

| 键 | 说明 |
|---|---|
| `public_url` | 机器人回复的服务器地址；不配则用代码默认生产地址 `http://101.201.108.188:8787` |
| `device_token_hours` | 新令牌有效期；默认 720；`0` = 10 年 |
| `token_groups` | 允许签发的群号；键不存在时回退 `statistics.allowed_groups`；显式空数组=关闭群签发 |
| `token_users` | 允许签发的私聊 QQ；键不存在时回退 `statistics.private_users`；显式空数组=关闭私聊签发（管理员仍可管理） |
| `token` | 旧总令牌；不要复用 QQ/NapCat/统计 token |

当前生产未改 `config.json`：`public_url` 走默认，`device_token_hours` 走 720，`token_groups`/`token_users` 走统计名单。  
如果要改这些键，只需重启 `ygo-qq`（命令在 QQ 进程）；改 `watch_api`/`watch.py` 才需要重启 collector。

## 7. 部署与回滚

### 7.1 文件 → 服务

| 文件 | 服务 |
|---|---|
| `watch.py` | `ygo-collector` |
| `bot/watch_api.py` | `ygo-collector` |
| `bot/watch_tokens.py` | `ygo-collector` + `ygo-qq` |
| `bot/store.py` | `ygo-collector` + `ygo-qq` |
| `bot/watch_token_command.py` | `ygo-qq` |
| `bot/commands.py` | `ygo-qq` |
| `bot/service.py` | `ygo-qq` |
| `bot/help.py` | `ygo-qq` |

`ygo-web` 不需要重启：`watch_tokens` 是新增表，网页不读它。

### 7.2 上线步骤

1. 备份要覆盖的线上文件：`*.bak-YYYYmmdd-HHMM`。
2. 确认 `data/capture-status.json` 的 `active_observations=0` 且 `updated_at` 新鲜；不要在有在录对局时重启 collector。
3. 停 `ygo-qq`、`ygo-collector`。
4. 安装文件，`chown ygo:ygo`，`python3 -m py_compile`。
5. 启动 `ygo-collector`，确认 8787 `/health` 200、日志出现 `每设备令牌：已启用`。
6. 启动 `ygo-qq`，确认 `active`、NapCat 已连接。
7. 用旧总令牌 + 临时每设备令牌做接口验证（见第 9 节），测试行用完删除/吊销。
8. 更新交接文档。

### 7.3 2C2G / 磁盘约束（重要）

这台生产是 **2 vCPU / 2GB RAM**，磁盘 40GB。2026-11-03 21:39 上线后的实测：

| 项 | 数值 |
|---|---|
| 内存 | total 1740MB，used 1464MB，available **276MB**；部署前低到 **146–160MB** |
| 磁盘 | 40GB，used 12GB，available **26GB**（32%） |
| 数据库备份 | 本次新增一份 **545MB** 的 `bot-before-watch-tokens-...sqlite3` |
| 进程峰值 | collector **307.7MB**、qq **24.4MB**（systemd 停止时记录） |

这不是“内存/磁盘很宽裕”的机器。部署前我对 545MB 的数据库备份跑了 `PRAGMA integrity_check`，造成磁盘 I/O 压力、SSH 短暂 banner 超时；检查进程结束后服务未受影响，但这是一次实际教训。

**操作红线：**

1. **available 低于 200MB**：不做非必要备份、全库校验、批量日志导出或多个服务同时重启；低于 **150MB** 不要重启 collector，先等内存恢复或只重启 qq。
2. **磁盘可用低于 2GB**：停止新的全量备份/部署；低于 **1GB** 按故障处理，先清旧备份/旧日志，**不要删** `bot.sqlite3`、`bot.sqlite3-wal`、`bot.sqlite3-shm`、`data/replays`。
3. **不要对 545MB 备份跑 `PRAGMA integrity_check` / `quick_check` 全量扫描**；也不要在生产库上跑 `VACUUM`。需要验证备份只做文件大小/时间检查，或轻量 `PRAGMA schema_version`。
4. **不要用 `cp` 热复制正在写的 `bot.sqlite3`**（WAL 可能不一致）；用 SQLite `.backup` API，或停服务后复制。一次只保留一份待确认的全量备份，确认新备份可打开后再删旧快照。
5. **串行操作**：2 vCPU 下不要并行跑全量测试、备份、索引和重启；令牌签发本身很轻，但不要在低内存时同时做整库备份。
6. **磁盘不是无限**：`data/backup`、`data/code-backups`、旧 `bot*.sqlite3` 快照、`bot/*.bak-*` 都会累积；清理前先 `ls -lh`/`du -sh`，确认不是当前库/唯一备份。`/tmp` 与根分区同盘，不要把大 tar/整库复制到 `/tmp`。
7. 例行检查只用轻量命令：

```bash
free -m
df -h /opt/ygobot
du -sh /opt/ygobot/data/backup /opt/ygobot/data/code-backups /opt/ygobot/data/logs 2>/dev/null
```

`watch_tokens` 本身很小（每行只有哈希和元数据），令牌功能不是磁盘增长的主要来源；真正的空间大头是数据库、录像和旧备份。

### 7.4 回滚

- 恢复 6 个旧文件（`watch.py`、`watch_api.py`、`store.py`、`commands.py`、`service.py`、`help.py`）的 `.bak`。
- 新增文件 `watch_tokens.py`、`watch_token_command.py` 可以保留：旧代码不会 import 它们。
- `watch_tokens` 表可以保留：旧代码不读；它是 additive，不影响旧表。
- 重启 `ygo-collector` + `ygo-qq`；确认 health/NapCat。
- 如果只是命令/名单配置问题，优先只重启 `ygo-qq`，不要动 collector。

## 8. 管理员运维

### 8.1 查看

管理员私聊：

```text
令牌分发 列表
```

输出示例（只有 hint，没有完整令牌）：

```text
观战令牌（有效 2 个）：
android-123456789  QQ 123456789  wt_abcd…wxyz  到期 2026-12-03 21:40
android-123456789-2  QQ 123456789  wt_efgh…ijkl  到期 2026-12-03 21:41
```

列表最多显示最近 30 个；已过期/已吊销不显示。

### 8.2 吊销

```text
令牌分发 吊销 android-123456789
令牌分发 吊销 QQ 123456789
```

- 按客户端名称：只吊销该 worker 的有效令牌。
- 按 QQ：吊销该 QQ 的所有槽位。
- 吊销后下一次请求立即 401；不需要重启 collector。

### 8.3 常见处置

| 场景 | 处置 |
|---|---|
| 用户丢失令牌 | 让用户重发 `令牌分发`（旧令牌自动失效） |
| 令牌泄露到群外 | 管理员吊销该 worker 或 QQ，让用户重发 |
| 用户换手机 | 重发同一槽位即可；客户端名称不变 |
| 用户要第二台设备 | `令牌分发 2`；客户端名称用 `...-2` |
| 旧 PC/云端掉线 | 总令牌仍有效；不要因为安卓令牌问题重启 collector |
| 需要停用某群 | 从 `token_groups`/`statistics.allowed_groups` 移除群号，重启 `ygo-qq` |

### 8.4 只读排查 SQL

```sql
-- 只看元数据，不查 token_hash 之外的秘密
SELECT id, worker, owner, datetime(expires_at,'unixepoch','localtime') AS expires,
       datetime(revoked_at,'unixepoch','localtime') AS revoked, revoked_by
FROM watch_tokens
ORDER BY id DESC LIMIT 20;
```

不要 `SELECT token_hash` 后外发；虽然不能还原明文，也没有必要。

## 9. 验收与测试

### 9.1 自动化

```powershell
py -3 -m unittest discover -s tests -p "test_watch_device_tokens.py" -v
```

覆盖：

- 只存 SHA-256，不存明文；
- 绑定 worker、错误 worker 403；
- 过期、吊销、重发吊销旧令牌；
- 安卓 APK 的实际请求链路：`status -> claim -> heartbeat -> upload -> report`；
- QQ 群内自动回复、保密名单允许/拒绝、管理员列表/吊销；
- 旧 `remote.token` 仍可用。

2026-11-03 结果：新测试 23 项通过；目标相关回归 88 项通过。  
证据（项目工作区）：`workflow-results/watch-device-tokens-tests-20261103.log`。

### 9.2 生产验证（不打印令牌）

```bash
# 旧总令牌
legacy_status 200
# 每设备令牌
device_status 200
# 错误客户端名
wrong_worker_claim 403
# 正确客户端名（无任务时 204）
correct_worker_claim 204
# 吊销后
revoked_status 401
```

2026-11-03 生产实测即以上结果；测试行已删除，`watch_tokens` 行数 0。  
证据（项目工作区）：`workflow-results/watch-device-tokens-production-20261103.txt`。

> 真实 QQ 群消息尚未发送，因此“真实群端回复”还需群友发一次 `令牌分发` 验收；自动化/生产接口验证不等同于真实 QQ 消息验收。

## 10. 排障

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 群里发 `令牌分发` 没反应 | 群命令未启用/需要 @；用户无权限；群不在名单 | 检查 `qq.group_commands`、`group_without_at`、`qq.allow_all_users/users`、`statistics.allowed_groups` |
| 回复“只对保密名单内的群/用户开放” | 群号/QQ 不在名单；或 `token_groups`/`token_users` 显式为空 | 加名单，重启 `ygo-qq` |
| 回复“令牌刚发过” | 30 秒冷却 | 用上一条；等 30 秒；不要连续刷 |
| APP 显示“令牌无效或已过期” | 令牌过期/吊销/抄错；或客户端名称不一致（403） | 重发 `令牌分发`；核对客户端名称完全一致 |
| `claim` 一直 403 | 客户端名称与令牌绑定值不同 | 改成机器人回复的名称；不要用 `android-1` |
| `claim` 204 | 当前没有任务 | 正常长轮询，不是错误 |
| 上传 403 | job 已被别的 worker 认领，令牌不匹配 | 用绑定该 worker 的令牌；重发 |
| 管理列表为空 | 没有有效令牌（都过期/吊销） | 让用户重发；列表只显示有效项 |
| “令牌库暂时不可用” | SQLite busy/磁盘/权限 | 查 `journalctl -u ygo-qq`、磁盘、`data/bot.sqlite3` 权限 |
| 旧 PC/云端也 401 | 总令牌被轮换/配置读错 | 检查 `remote.token`；不要因安卓问题改总令牌 |
| 命令只在私聊有效，群里无效 | 群不在允许名单，或 `group_commands=false` | 加群号，重启 `ygo-qq` |

## 11. 已知限制

- 真实 QQ 群消息验收仍待用户执行一次 `令牌分发`。
- 只有 1–9 个自动命名槽位；不支持在命令里自定义 worker 名称。
- 令牌不记录 `last_used_at`，也没有设备指纹；Bearer 被复制即可用（但受 worker 绑定限制）。
- 群内自动发布意味着同群成员可见令牌；这是小群便利性取舍，不适合大群/公开群。
- 旧 `remote.token` 仍是全局总令牌，权限最大；泄露时需单独轮换，会影响所有旧客户端。
- `public_url` 缺省硬编码当前生产地址；换域名/端口时需配置。
- 过期行不会自动清理；表增长很慢，必要时手动归档/删除。
- 生产是 2C2G/40GB 低资源机：磁盘 545MB 级备份、低可用内存和全库校验都可能拖垮 I/O；操作红线见 7.3。
- QQ 命令的少部分管理报错文案仍写作“观战令牌”（旧称），实际命令以 `令牌分发` 为准。

## 12. 相关文件

- 代码：`bot/watch_tokens.py`、`bot/watch_token_command.py`、`bot/watch_api.py`、`watch.py`、`bot/store.py`
- 接入：`bot/commands.py`、`bot/service.py`、`bot/help.py`
- 测试：`tests/test_watch_device_tokens.py`
- 配置模板：`config.example.json` 的 `remote` 段
- 安卓指南：`docs/ANDROID_CLIENT.md`
- 接口契约：`docs/REMOTE_WATCH.md`
- 部署流程：项目工作区 `docs/DEPLOY.md`（主仓库只带运行时代码/开发文档，不含该运维文件）
- 交接：项目工作区 `PROJECT_HANDOFF.md`
