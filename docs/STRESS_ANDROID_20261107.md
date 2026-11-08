# 安卓端容量上限与带宽结论（2026-11-07）

- 测试对象：用户提供的 `戏王版权局-v2.0.apk`（`versionCode 20` / `versionName 2.0`，debug 包 `com.moecard.watch.debug`）；GitHub `usertools_android` 最新为 1.9.1（`6c17e4e`），比线上落后一个版本。
- 线上真实客户端：`/query-api` UA 全是 `MoeCardQuery/1.3`、`/watch-api` 为 `MoeCardWatch/1.2`。v2.0 的 dex 里只有这两个固定 UA；1.9.1 的查询 UA 会写 `MoeCardQuery/1.9.1`，日志里一次都没出现——**线上跑的是 2.0 这一代，不是 1.9.1，也不是更早的包**。
- 方法：只读采样（SSH 快照、nginx access log、`statistics-query-timings.jsonl`、cgroup/PSI）+ 对 v2.0 APK 做 dex 静态分析，**未发压、未重启、未改配置、未签发测试账号**。线上 11:18–11:41 另有会话在做性能剖析，本轮刻意避开。

---

## 0. 先给上限数量（结论）

| 维度 | 现状稳定上限 | 实测/依据 | 只修内存（2C4G） | 修内存 + roster 移出全局锁 | 4C4G + 上述 |
|---|---|---|---|---|---|
| **同时查询用户** | **1 人**（2 人开始明显失败） | 1 IP/分钟：503 占 16.7%；2 IP/分钟：44.3%；11:10–11:40 有 6 个以上公网 IP 同时使用，11 点这一小时 98 次请求、33 次 503（34%） | 2–3 | 8–12 | 20–30 |
| **在线观战设备** | **30–50 台** | 24h 约 9.0 万次 `/watch-api`、22.7 MB；8787 全站 41.5 MB；当前 12 个公网 IP；collector CPU 12% | 同左 | 50–80 | 100+ |
| **同时录制房间** | **8–10 路** | 现网 collector 5s 采样 12%；历史 8 局在录约 109% CPU（2 核） | 10–12 | 10–12 | 16–20 |
| **同时下载录像** | **1 路** | 全局限速 200 KiB/s ≈ 1.6 Mbps，占 3 Mbps 的 53%；4 路并发但共享同一个桶 | 1–2 | 2 | 3–4 |
| **查询带宽理论上限** | **300–700 人** | top100 gzip 15 KB；3 Mbps ≈ 25 次/秒 = 1500 次/分钟；按每人 2–4 次/分钟 | — | — | — |

> 观战设备数以上限“总房间数 ≤8–10”为前提：每台设备最多领 6 间，设备多、房间少时由带宽/连接数决定；房间多时先撞录制 CPU。两个数字不能同时取满。

一句话：**现在卡住的是内存和一把全局计算锁，不是带宽。** 3 Mbps 出口 24 小时峰值一分钟 327 kbps（**10.9%**），全天平均 4.2 kbps。

---

## 1. 线上基线（2026-11-07 11:38–11:45）

| 项 | 数值 | 说明 |
|---|---|---|
| 整机 | 2 vCPU / 1740 MB，load 2.67 | `available` 469–479 MB |
| Swap | **1004 / 2047 MB** | qq 186 MB、web 161 MB、replay-api 134 MB、collector 38 MB 被换出 |
| 内存 PSI | some avg300 **17.98%**，full avg300 **15.59%** | full 15% 表示整机约 15% 时间卡在回收上 |
| web cgroup | current 427 MB / high 420 MB / max 500 MB，`memory.high` 事件 **31,983** | 长期贴着 high 水位跑 |
| collector / web CPU | 12% / 7%（5 秒采样） | 录制不是当前瓶颈 |
| 数据库 | 1007 MB；WAL 287 MB | 距 512 MB 告警线不远 |
| 磁盘 | 21 GB 可用 | 正常 |

---

## 2. 上限是怎么算出来的

### 2.1 查询：一把锁 + 30–90 秒的 roster

服务端结构（`statistics_portal.py`）：

- 所有重查询走同一个 `_query_compute_lock`，**等 8 秒拿不到就返回 503**（nginx 日志里这些 503 的响应体恒为 43 字节）；
- `roster`（top100 / 饼图的数据源）在锁内重算，今天 00:03–11:39 共 **118 次**（约 10 次/小时，即每 6 分钟一次），**p50 30.5 s、p90 91.3 s、p95 102.8 s、max 126.3 s**（`statistics-query-timings.jsonl`，仅统计成功样本）；按 30 秒/次估算，锁约有 **8–9% 的时间被占**，且集中在用户刚打开 App、缓存刚过期的那一刻；
- `deck-detail` 反而很轻：p50 0.58 s、p95 5.2 s；
- 闸门：轻请求 6 并发、重请求 2 并发，等待 0 秒，满了直接 429（客户端 1/2/4/8 秒退避）。

因果链：

```
有人打开前百/饼图 → roster 缓存过期（300s TTL）→ 后台线程重算 30–90 秒
  → 整个 30–90 秒里 _query_compute_lock 被占
  → 其他人点玩家/卡片 → 等 8 秒 → 503（43 B）
  → 旧客户端每 7–11 秒重试 → 503 越积越多
```

实测（今日 00:00–11:45，仅 UA=MoeCardQuery）：

| 每分钟不同 IP 数 | 请求数 | 200 | 503 | 503 占比 |
|---:|---:|---:|---:|---:|
| 1 | 269 | 220 | 45 | **16.7%** |
| 2 | 61 | 31 | 27 | **44.3%** |

11:10–11:40 的突发：日志里出现 7 个不同公网 IP（112.224.7.246、119.143.66.11、144.12.247.131、112.52.4.247、175.2.169.139、175.13.213.148、39.170.111.11）；11 点这一小时 98 次请求、33 次 503（34%）。同一个路径最多连续 18 次 503（`112.224.7.246` 的 `灵sama/decks`）。

> 注：失败里含客户端重试，用户实际“点一次”的失败率低于 503 占比；但 1 个人用也会每 6 次失败 1 次，已经不是“人多才卡”。

**为什么 roster 要 30–90 秒**：同一份代码 11-07 00:25 的基线是 10.7 秒；现在整机 swap 1 GB、PSI full 15%，计算被内存回收拖慢 3–8 倍。这不是算法退化，是内存不够。

### 2.2 观战与控制面：真正的“轻”

24 小时：`heartbeat` 63,953、`claim` 13,276、`status` 6,554、`upload` 5,691、`report` 1,086，合计 **90,560 次、22.67 MB**（status 响应 19.0 MB，占 84%）。

- 单客户端（1.9.1 补丁后）：status 6.7 KB/60 s ≈ **0.9 kbps**，heartbeat 可忽略；**100 台 ≈ 90 kbps，占 3 Mbps 的 3%**；
- 实测 12 个公网 IP 分摊约 **0.17 kbps/台**（按 24h 实测均值；status 间隔未抓包）
- 上传一局录像+旁证约 5.2 KB，100 局/天 ≈ 0.5 MB/天。

所以观战端的数量上限不在带宽，而在：① 录制占 CPU；② 平台每公网 IP 6–7 路；③ 单机多账号。

### 2.3 带宽：实测只用掉 2%

| 口径 | 数值 |
|---|---|
| 24h 8443 查询流量 | 3.87 MB（824 次） |
| 24h 8787 流量 | 41.51 MB（94,953 次） |
| 其中 `/watch-api` | 22.67 MB（90,560 次） |
| 峰值一分钟出口 | 2.45 MB ≈ **327 kbps = 3 Mbps 的 10.9%** |
| 全天平均 | 4.2 kbps（0.14%） |

单次传输耗时（3 Mbps = 375 KB/s）：

| 响应 | gzip 后 | 传输时间 |
|---|---:|---:|
| top100 | 15.2 KB | **0.04 s** |
| 饼图 | 3.2 KB | 0.01 s |
| 玩家构筑（网页同源） | 21 KB | 0.06 s |
| 卡片统计（网页同源） | 22 KB | 0.06 s |
| 未压缩卡片统计 | 563 KB | 1.5 s |

也就是说：**用户等的是 8 秒锁 + 30–90 秒计算，不是 0.04 秒传输。** 带宽再翻一倍，体验几乎不变。

gzip 已生效并可验证：`top100` 在 11-06 19:00 前中位 72 KB，19:00 后中位 15.2 KB（4.8×）。

### 2.4 v2.0 包静态分析（未装真机）

| 项 | 值 |
|---|---|
| 包名 / 版本 | `com.moecard.watch.debug` / `versionCode 20`、`versionName 2.0`、minSdk 26、targetSdk 34 |
| 签名 | debug 证书 `49c5c9ec…`；与 1.9.1 测试包 `c271c797…` **不同**，不能覆盖安装（要么卸载丢配置，要么用原 keystore 重签） |
| 查询接口 | 与 1.9.1 相同：top100、charts/pie、search、player/*、card/*、subscriptions、login/refresh/logout/me |
| 新增 | `/replay-api/matches?`、`/replay-api/download?id=`（App 内录像库，与 PC v1.5 同款） |
| 客户端缓存 | 有 `RosterCache` 类（前百缓存），可减少重复 top100 |
| 卡图 | dex 里没有 `!thumb2`，仍取原图（走第三方 CDN，不占服务器出口） |
| UA | 固定 `MoeCardQuery/1.3`、`MoeCardWatch/1.2` |

对容量的影响：新增录像库多一条下载出口，但仍受 200 KiB/s 全局桶限制；查询侧没有新增重接口，`RosterCache` 只会减少请求。**v2.0 不改变“瓶颈在内存和锁”的结论。**

---

## 3. 带宽相关要怎么做

### 3.1 先说不做什么

1. **现在不升带宽。** 峰值 327 kbps，升到 5 Mbps 只是把 10.9% 变成 6.5%，对查询延迟没有可感知贡献。
2. **不要取消下载限速。** 200 KiB/s 是唯一真正吃出口的东西（占 53%），取消后一次下载就能把查询/心跳挤到队尾。
3. **不要为提高压缩比调高 `gzip_comp_level`。** level 1 已到 4.8×；2 vCPU 上再压 JSON 抢的是计算，不是带宽。

### 3.2 按收益排序的动作

| 顺序 | 动作 | 省什么 | 现状/预期 | 成本 |
|---:|---|---|---|---|
| 1 | ✅ nginx gzip（已上线） | top100 72→15 KB、card 563→22 KB | 已验证 | 0 |
| 2 | 只读接口加 `Cache-Control: private, max-age` + `ETag`/304 | 重复打开同一页从 15 KB 降到 ~0.3 KB | 未做；服务端仍统一 `no-store` | 低（改 `query_api._send`） |
| 3 | 客户端内存 TTL（前百 60 s、饼图 300 s、卡片 600 s） | 切页/回退不重复拉 | 1.9.1 没有 | 低（发版） |
| 4 | keep-alive（去掉 `close_connection=True`，nginx 配 upstream keepalive） | 每请求省 2–4 KB 握手 + 1–2 RTT | 现在每请求新连接 | 中（要改连接生命周期） |
| 5 | 卡图 `!thumb2`（第三方 CDN，不占服务器出口） | 前百卡墙 0.6–1.8 MB → 35–70 KB | 冻结项，未授权 | 低（但需明确解冻） |
| 6 | 下载桶维持 200 KiB/s；多人下载排队而不是各分 50 KiB/s | 保证查询/心跳余量 | 现状 4 路共享桶 | 低 |

### 3.3 什么时候才需要升带宽

满足任一条件再考虑 3 → 5 Mbps：

- 连续 10 分钟出口 > 1.5 Mbps（现在峰值 0.33 Mbps，差 4.6 倍）；
- 单日 8787+8443 出站 > 10 GB（现在约 45 MB/天，差 220 倍）；
- 同时 3 路以上录像下载成为常态；
- 查询用户稳定 > 30 人且 503 已修完（这时才会开始碰 CPU，而不是带宽）。

**同样的钱，先加内存。** 2C2G → 2C4G 能把 roster 从 30–90 秒拉回 10 秒量级，直接决定 1 人还是 10 人可用；加 2 Mbps 只影响 0.04 秒的传输。

> **2026-11-07 12:30 约束更新：机器不能升配，内存固定 2 GB。** 替代方案见 §5.2；结论不变——带宽仍然不加，先做内存腾挪和 roster 出锁。

---

## 4. 针对 v2.0 的压测方案（待执行）

现在的线上数据已经来自 2.0（UA 1.3），且服务器处于内存紧张状态；此时加压只会复现 503，测不出真实上限。建议顺序：

1. **先修内存**（4 GB 或把 qq/replay-api 迁走），确认 PSI full < 5%、roster 回到 10 秒级；
2. 在低峰窗口（建议 03:00–06:00，且无真人使用）执行分级压测；
3. 每级之间留 60 秒让缓存/队列回落。

### 4.1 负载模型（按 1.9.1 源码；v2.0 端点集合相同，另加录像库）

| 阶段 | 并发虚拟用户 | 每用户请求序列 | 思考时间 |
|---|---:|---|---|
| S1 | 1 | top100 → pie → player decks → player replays → card | 2 s |
| S2 | 2 | 同上 | 2 s |
| S3 | 4 | 同上 | 2 s |

- 全部带 `Accept-Encoding: gzip`、`Authorization: Bearer <测试账号>`；
- 客户端串行（1.9.1 是 `Semaphore(1)`），不要并发打同一用户；
- 每阶段 180 秒或每接口 ≥30 个样本。

### 4.2 指标与中止线

| 指标 | 中止线 |
|---|---|
| 503 / 429 比例 | > 20% 立即停 |
| p95 端到端 | > 20 s 停止加级 |
| 整机 available | < 300 MB 立即停 |
| PSI memory full avg60 | > 30% 立即停 |
| load1 | > 6 立即停 |
| WAL | 单阶段涨 > 100 MB 停止 |

采样子进程每 5 秒记录：available、PSI、load、web RSS、WAL、nginx 5xx、collector CPU。

### 4.3 需要的前置条件

- 一个**测试查询账号**（管理员 issue，测完 disable；不复用真人账号）；
- 与另一个性能剖析会话错开（本轮 11:18–11:41 有剖析在跑）；
- 用户确认窗口；不重启任何服务。

---

## 5. 建议顺序（今天 / 本周 / 下次发布）

| 优先级 | 动作 | 理由 |
|---|---|---|
| P0 | 内存腾挪：web `MemoryHigh` 480M / `MemoryMax` 560M + zram 1G（lz4） | 不能升配；先止住 swap 抖动，见 §5.2 |
| P0 | roster 预计算到文件 / 独立进程 + `malloc_trim` | 30–90 秒的锁和 250–300 MB 中间对象都出 web |
| P1 | 只读接口缓存头 + 客户端 TTL | 带宽/重复请求减半，成本低 |
| P1 | 503 排队 2 秒再返回，而不是立刻 8 秒超时 | 吸收瞬时并发 |
| P2 | keep-alive、查询进程独立 | 延迟与隔离 |
| P3 | 带宽 3 → 5 Mbps | 等出口真的 >1.5 Mbps 再说 |

> `LOGIC_FREEZE.md` 冻结的是取源/口径/算法；上面 P0/P1 只动并发与缓存结构，不动统计口径，但仍需用户明确授权后才实施。

### 5.2 2 GB 不扩容的可行方案（2026-11-07 12:30）

现状（另一会话 250 MB 测试进程在跑时）：web cgroup **439 MB 常驻 + 369 MB 已换出**、`memory.high` 事件 60,957；collector 359 MB + 109 MB swap；qq 15 MB + 237 MB swap；replay-api 4 MB + 138 MB swap；swap 2 GB 用 1.0 GB，si/so 各 3–4 MB/s，iowait 73–81%、PSI full 30%。

根因不是“总量差很多”，而是 `MemoryHigh=420M` 把 web 的热页也压进 swap，读回来又是随机 I/O。

**A. 立刻（零重启、可回滚）**

```bash
# 包内脚本：drop-in 写 MemoryHigh=480M/Max=560M + 运行时 oom_score_adj + 1G zram
bash stability/mem2g-20261107/01-memory.sh
bash stability/mem2g-20261107/01-memory.sh --status
```

> 不要用 `systemctl set-property` 设完再 `daemon-reload`：会被 `memory.conf` 改回 420M/500M（14:26 实际踩到）。`OOMScoreAdjust` 也不接受 `set-property`，脚本改为写 `/proc/<pid>/oom_score_adj` + drop-in 持久化。

观察 15 分钟：`vmstat 1`（si/so、wa）、`/proc/pressure/memory`、web 的 `memory.swap.current`、下一次 roster 耗时。
预期：web 的 369 MB 交换页大部分回到 RAM/zram，iowait 降到 20% 以下，roster 回到 10–20 秒。

回滚：`systemctl set-property ygo-web.service MemoryHigh=420M MemoryMax=500M`；`swapoff /dev/zram0; modprobe -r zram`。

**A 已应用（2026-11-07 14:26）**：web 480M/560M + `oom_score_adj=200`、collector `-500`、zram 1G lz4。15 分钟观察：zram 104–178 MB、swapfile 683→643 MB、`available` 157→约 400–600 MB、PSI some 0–4%、`si/so` 3–4 MB/s → 0–0.5 MB/s、0 OOM/0 重启、四个服务 active。web 在 roster 期间仍会摸到 480M 上限（473 MB + 160 MB swap），要等 B 才能消除。

**B 已应用（2026-11-07 15:00–15:01）**：`tools/roster_precompute.py` + 4 分钟 timer，portal 补丁 rebase 到另一会话的新基线 `a4e2552b…`（新增 `deck-pie.png` 路由）后应用，打后 `3073f8d6…`。验证：roster 响应带 `X-Statistics-State: precomputed`；15:00 后 web 的 roster 计时 **0 条**；deck-pie.png 200（复用预计算 roster）；15:00–20:50 查询 503 共 **7 次**，全部是 deck-detail，无 roster 相关（对照 11 点 34 次/107）。web 仍常贴 480M（card-report 12 s 级），是下一个优化点。

风险：web 涨到 560 MB 会挤压 collector，建议同时给 collector 加 `OOMScoreAdjust=-500`（当前未设置）；zram 压缩占 CPU，但换掉的是 iowait，2 vCPU 下 1 GB 比 2 GB 稳。

**B. 本周（代码，收益最大）**

1. roster 预计算：systemd timer 每 5 分钟跑一次性进程算好写 `data/cache/roster.json`，web 只读文件；进程退出即释放 250–300 MB，也不再占 `_query_compute_lock`；
2. 重计算后调 `malloc_trim(0)`，把空闲 arena 还给 OS；
3. 重查询 503 前排队 2 秒。

预期：web 常驻 439 → 250–300 MB；503 从“每次 roster 30–90 秒”变成“只在文件过期时短暂等待”；查询上限 2–3 人 → 5–8 人。

**C. 仍然不做**：升带宽、动 QQ/NapCat（观察期）、改统计口径。

补丁包已写好、**未应用**：[`stability/mem2g-20261107/`](../stability/mem2g-20261107/README.md)。
- 生产 dry-run：roster 18.3 s / 76.6 KB / 峰值 RSS 227 MB（nice 15，只读）；
- 目标文件基线 `e8d77d63…`，补丁后 `1cfb75c6…`；`03-apply.py` 先校验 hash，不一致直接拒绝；
- 注意生产文件是 CRLF，不能用 `patch`/`git apply`，用包内 Python applier。

---

## 6. 未做与边界

- 未发压、未重启、未改 nginx/服务端配置、未签发账号、未安装 1.9.1 APK 到真机。
- `roster` 的 30–90 秒是**含内存回收拖慢**的现值，不是纯 CPU 上限；4 GB 后需复测。
- 上表的“修复后”列是规划值，不是压测结论；只有 S1–S3 跑完才能写成实测上限。
- v2.0 只做了 dex 静态分析，未真机安装/抓包；其 status 间隔、重试预算是否与 1.9.1 相同尚未验证。

---

## 7. 证据

| 内容 | 位置 |
|---|---|
| 本轮基线快照 | 2026-11-07 11:38–11:45 SSH 只读采样（本文 §1） |
| 查询状态/字节/时间序列 | `/var/log/nginx/access.log`（8443）、`ygobot.access.log`（8787） |
| roster/deck 耗时 | `/opt/ygobot/data/logs/statistics-query-timings.jsonl` |
| 锁与 503 文案 | `/opt/ygobot/bot/statistics_portal.py`（`QUERY_WAIT_SECONDS=8.0`、`_query_busy_reply`） |
| 客户端请求序列 | `.tmp-android-latest/.../domain/WatchClient.kt`、`query/net/QueryApi.kt`、`ui/query/*` |
| 历史带宽分析 | `docs/BANDWIDTH_ACCESS_ANALYSIS_20261106.md`、`docs/BANDWIDTH_P0_NGINX_GZIP_20261106.md` |
