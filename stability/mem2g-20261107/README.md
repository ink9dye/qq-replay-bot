# mem2g：2 GB 不扩容的内存 / roster 补丁包（2026-11-07）

> 目标：机器固定 2 GB、不能升配、不停机的前提下，把查询从「1 人可用」拉回「几个人可用」。
> 本包**只写补丁，不执行**。生产上还有别的会话在改代码，应用前必须过 §3 的 hash 门。

## 0. 一句话

不能加内存，就把 **roster 那 30–90 秒计算、250–300 MB 中间对象、以及它占的全局锁** 从 web 进程搬到一个每 4 分钟跑一次的一次性进程里；同时让 web 的热页不再被 `MemoryHigh=420M` 压进 swap（配 zram）。带宽不动。

## 1. 依据（本轮实测）

| 项 | 数值 |
|---|---|
| web cgroup | 439 MB 常驻 + **369 MB 已换出**，`memory.high` 事件 **60,957** |
| collector | 359 MB + 109 MB swap |
| qq / replay-api | 15 MB + 237 MB swap；4 MB + 138 MB swap |
| swap I/O | si/so 各 3–4 MB/s，`wa` 73–81%，PSI full 一度 30% |
| roster | 今天 118 次，p50 **30.5 s**、p90 91.3 s，全程占 `_query_compute_lock` |
| 结果 | 1 个 IP 时 503 占 16.7%，2 个 IP 44.3% |

## 2. 包里有什么

| 文件 | 作用 | 改现有代码？ |
|---|---|---|
| `01-memory.sh` | web `MemoryHigh=480M/Max=560M`、collector `OOMScoreAdjust=-500`、1 GB zram（lz4）；带 `--status` / `--rollback` | 否（systemd drop-in + 内核） |
| `02-roster-precompute/roster_precompute.py` | 一次性进程算 roster，原子写 `data/cache/roster.json` | 否（新增文件） |
| `02-roster-precompute/*.service` / `*.timer` | 每 4 分钟跑一次预计算 | 否（新增 unit） |
| `02-roster-precompute/install.sh` | 安装上面三项；目标文件已存在则拒绝 | 否 |
| `03-apply.py` | 给 `bot/statistics_portal.py` 打补丁：读预计算文件 + `malloc_trim` | **是**（1 文件 / 3 处） |
| `03-apply.sh` | 上面的薄封装 | — |
| `03-portal.diff` | 补丁的可读 diff（仅供参考，不用来打补丁） | — |
| `statistics_portal.py.base` | 基线全文（sha256 `a4e2552b…`） | — |
| `statistics_portal.py.new` | 打完后的全文（sha256 `3073f8d6…`） | — |
| `check.sh` | 只读核对 hash / roster 年龄 / timer / 内存 | — |
| `04-card-samples.py` | 卡牌 `metrics.samples` 默认截到 200 条、网页 `full=1` 保留全量；hash 门 + 备份 + 回滚 | **是**（portal + 1 个 JS） |
| `05-card-investment.py` | app 路径不再返回 `investment`（76 KB；安卓不读），网页按 `full=1`/浏览器 UA 保留 | **是**（portal） |
| `06-card-cache.py` | `card-metrics` 进程内缓存（按 query，TTL 600 s）；冷计算 10 s → 命中 0.01 s | **是**（portal） |
| `07-card-speed.py` | `eligible_views` 文件存在性缓存 60 s + 无对手筛选时跳过对手查询；`card_query` 6.4 s → 1.8–2.6 s | **是**（top100 + card_statistics） |

**重要**：生产 `statistics_portal.py` 是 **CRLF** 换行，不要用 `patch`/`git apply` 直接打；`03-apply.py` 会保留原换行、先校验 hash、再备份。

## 3. 冲突规则（先看这节）

- 目标文件基线 sha256：`a4e2552b7e1c08c02fe536813d282fbb456e703e6771a369c49c138e8d9decff`。
- 2026-11-07 15:00 基线从 `e8d77d63…` 更新为 `a4e2552b…`：另一会话新增了 `/statistics/api/deck-pie.png` 路由（内部也调 `_roster_reply()`，与本补丁兼容）。旧基线全文保留为 `statistics_portal.py.base-v1230`。
- `03-apply.py` 发现 hash 不一致会 **直接退出 3**，不尝试合并、不覆盖。
- 包内不碰：`query_api.py`、`statistics_refresh.py`、`statistics_server.py`、`config.json`、nginx、Android、QQ。
- 只新增 `/opt/ygobot/tools/roster_precompute.py`；若已存在，`install.sh` 拒绝。
- 如果另一个会话已经改了 `statistics_portal.py`：**不要用本包**，按 §7 把两处改动重新套到新版本上，再更新 hash。

## 4. 应用顺序

先把整个 `mem2g-20261107/` 目录拷到服务器（不要改服务器上其他文件）：

```bash
scp -r stability/mem2g-20261107 root@<server>:/root/
```

### A. 内存（独立、可回滚、不重启服务）

```bash
bash 01-memory.sh
bash 01-memory.sh --status
# 观察 15 分钟
vmstat 1 5; cat /proc/pressure/memory; swapon --show
```

预期：`si/so` 从 3–4 MB/s 降到 <0.5 MB/s，`wa` 20% 以下，web 的 `memory.swap.current` 明显下降。

**注意**：`MemoryHigh/Max` 必须写 `zz-mem2g.conf` drop-in（排在 `memory.conf` 之后）。用 `systemctl set-property` 设完再 `daemon-reload`，会被旧 drop-in 改回 420M/500M（14:26 实际踩到过）。`OOMScoreAdjust` 也不接受 `set-property`，脚本改为运行时写 `/proc/<pid>/oom_score_adj` + drop-in 持久化。

### B. 预计算（只加文件，先跑起来；此时 web 还不用它）

```bash
bash 02-roster-precompute/install.sh
# 先 dry-run 确认能算出来（30-90 秒）：
sudo -u ygo /usr/bin/python3 /opt/ygobot/tools/roster_precompute.py --root /opt/ygobot --dry-run
# 正式写一次：
sudo -u ygo /usr/bin/python3 /opt/ygobot/tools/roster_precompute.py --root /opt/ygobot
ls -l /opt/ygobot/data/cache/roster.json
```

### C. portal 补丁（要重启 web，挑无查询/无录制的窗口，需用户同意）

```bash
python3 03-apply.py --root /opt/ygobot --dry-run   # 只校验，不写
python3 03-apply.py --root /opt/ygobot             # 备份 + 打补丁 + py_compile
systemctl restart ygo-web
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/health
```

回滚：

```bash
python3 03-apply.py --rollback --root /opt/ygobot
systemctl restart ygo-web
bash 01-memory.sh --rollback
```

## 5. 验证与预期

```bash
bash check.sh
```

| 检查 | 预期 |
|---|---|
| portal hash | `3073f8d6…`（PATCHED） |
| `data/cache/roster.json` | 每 4 分钟 mtime 更新，`elapsed` 30–90 s |
| web cgroup | 439 MB → 250–300 MB；swap 369 MB → <100 MB |
| `statistics-query-timings.jsonl` | 不再新增 `"kind": "roster"`（web 不再算） |
| 查询 503 | roster 窗口内的 503 基本消失；card/matrix 仍可能偶发 |
| 容量 | 2–3 人 → 4–6 人（card/matrix 仍在锁内，下一包再拆） |

说明：文件超过 900 秒没更新时，web 自动回退到原来的进程内计算，不会 503 死；所以 timer 挂了最坏是回到现状。

### 已验证（2026-11-07 12:40–12:41，均未应用、未写入 roster.json）

| 项 | 结果 |
|---|---|
| 生产 dry-run（nice 15，只读） | **18.3 s**、body **76,610 B**、Max RSS **227 MB**，exit 0 |
| `03-apply.py --dry-run` 对线上文件 | hash 门通过，58,973 → 60,605 字节 |
| 本地沙箱 apply → rollback | 打后 sha256 `3073f8d6…` 与 `.new` 一致；回滚回到 `a4e2552b…` |
| Python 3.12.3 编译 | `03-apply.py`、`roster_precompute.py`、`.new` 全部通过 |

### A 的应用记录（2026-11-07 14:26 起，生产）

| 项 | 结果 |
|---|---|
| 生效配置 | web `MemoryHigh=480M`、`MemoryMax=560M`、`oom_score_adj=200`；collector `-500`；zram 1G lz4（prio 100），保留 2G `/swapfile` |
| 15 分钟观察 | zram 用 104–178 MB（压缩后约 35–55 MB）；`/swapfile` 683→643 MB；`available` 157→约 400–600 MB；PSI some 0–4%；`si/so` 从 3–4 MB/s 降到 0–0.5 MB/s；0 OOM、0 重启、四个服务 active、8787/8788 health 200 |
| 仍然存在 | web 在 roster 期间会摸到 480M 上限（current 473 MB + swap 160 MB），`memory.high` 事件仍增长——这是 B（roster 出进程）要解决的，A 只负责把交换变成 zram、把整机水位降下来 |
| 回滚 | `bash 01-memory.sh --rollback` |

踩坑记录：14:26 先用 `set-property` 设 480/560，随后为 OOM drop-in 执行 `daemon-reload`，`memory.conf` 把值改回 420/500；改为 `zz-mem2g.conf` 后生效。`set-property OOMScoreAdjust` 直接报 unknown property。

### B 的应用记录（2026-11-07 15:00–15:01，生产）

| 项 | 结果 |
|---|---|
| 安装 | `/opt/ygobot/tools/roster_precompute.py` + `ygobot-roster-precompute.{service,timer}`，每 4 分钟一次，nice 10 |
| 计算耗时 | 15:00 手动 9.7 s / 77,664 B / 227 MB RSS；timer 自动运行 3.5–38.7 s（随 DB 缓存/负载波动） |
| 基线变化 | 15:00 线上 portal 已被另一会话改为 `a4e2552b…`（新增 `/statistics/api/deck-pie.png`，内部也调 `_roster_reply()`）；补丁 rebase 后应用，打后 `3073f8d6…` |
| 验证 | `/statistics/api/roster` 200 + `X-Statistics-State: precomputed`；15:00 后 web 的 roster 计时 **0 条**；deck-pie.png 200（814 KB，复用预计算 roster） |
| 503 | 15:00–20:50 共 **7 次**，全部是 deck-detail 单飞等待（27.40.122.204、112.224.7.246 等），无 roster 相关；对照 11 点 34 次/107 |
| 内存 | web 379–479 MB + swap 111–208 MB，`max` 事件 0、OOM 0；仍常贴 480 high（card-report 12 s 级 + 页面请求），是下一个优化点 |
| 回滚 | `systemctl disable --now ygobot-roster-precompute.timer` → `python3 03-apply.py --rollback` → `systemctl restart ygo-web` |

### 卡牌 samples 截断（2026-11-08 22:29，生产）

- 背景：压测发现单次卡牌响应 2.11 MB / gzip 608 KB，`metrics.samples` 占 1.77 MB（6,645 条逐局）。
- 改动：`statistics_portal.py` 的 `card-metrics` 默认只留最近 200 条 + `samples_truncated`；`statistics_card_metrics.js` 加 `full=1`。只重启 web。
- 实测：`/query-api/card/45141013` gzip **607,961 → 33,452 B**（18×），`by_deck`/`investment` 不变；网页 full=1 仍全量。
- 版本：portal `df6f8d50…`、js `46957a3a…`；备份 `data/config-backups/card-samples-20261108/`；回滚 `python3 04-card-samples.py --rollback` + 重启 web。
- 浏览器需 Ctrl+F5（JS `max-age=3600`）。

### 卡牌 investment 省略（2026-11-08 22:47，生产）

- 安卓端不读 `investment`；query_core 走 urllib，UA 是 `Python-urllib`，portal 据此跳过 `player_investment` 与返回字段；浏览器/`full=1` 仍保留。
- 实测 portal：slim 180 KB / 带 investment 250 KB；`by_deck` 与 `samples` 不变。
- 版本：portal `ffa0ce7d…`；备份 `data/config-backups/card-investment-20261108/`；回滚 `python3 05-card-investment.py --rollback` + 重启 web。
- **暴露的既有问题**：`card-metrics` 单次 28–48 s，超过 query_core 8 s 超时，App 侧 `metrics` 会变 null；需先加 portal 结果缓存或降计算耗时。

### card-metrics 进程内缓存（2026-11-08 23:05，生产）

- `StatisticsPortal._card_metrics_cache`：按 `parsed.query`，TTL 600 s，最多 32 条 / 12 MB，单条上限 3 MB；命中直接返回 reply。
- 实测：portal 冷 10.4 s → 第二次 0.01 s；App 卡牌 0.60 s / 30,212 B gzip（metrics 有值），第二次 0.02 s；网页 full=1 6.2 s / 2.48 MB。
- 版本：portal `17a655cd…`；备份 `data/config-backups/card-metrics-cache-20261108/`；回滚 `python3 06-card-cache.py --rollback` + 重启 web。
- 边界：TTL 到期后的第一个请求仍约 10 s（>query_core 8 s），那一个用户会看到 null；可把 TTL 提到 1800 s 或降计算耗时。

### card_query 提速（2026-11-08 23:20，生产）

- `top100.eligible_views`：digest→文件存在性 + stat_files→rel_path 缓存 60 s（原每次 ~8.4k 次 stat）。
- `card_statistics.query`：无 `opponent_types/opponent_series` 时跳过对手行查询（默认请求省 1.6 万行）。
- 实测：`card_query` 6.4 s → **1.8–2.6 s**；portal 冷 10.4 s → **6.85 s**（低于 8 s 超时），缓存命中 0.01 s。输出不变（candidate 9,305 / samples 9,302；对手筛选仍可用）。
- 版本：`top100.py 438062dc…`、`card_statistics.py 7554146d…`；备份 `data/config-backups/card-speed-20261108/`；回滚 `python3 07-card-speed.py --rollback` + 重启 web。

## 6. 设计说明

- **为什么用文件而不是改 QueryRefresh**：`QueryRefresh` 的磁盘缓存只在内存条目为空时读一次，之后 web 会一直用自己的内存副本，外部刷新不会生效；显式文件路径才能每请求判断。
- **为什么 900 秒窗口**：timer 每 4 分钟一次，单次 30–90 秒；正常文件年龄 4–6 分钟。窗口放到 15 分钟，允许 timer 漏跑 1–2 次而不触发回退计算；超过才回退。
- **为什么保留回退**：timer/磁盘异常时不能把查询打死；回退路径就是现在的逻辑。
- **`malloc_trim`**：glibc 下把重计算释放的空闲 arena 还给 OS，减少 RSS 滞留；非 glibc 环境静默跳过。
- **zram 与 MemoryHigh 必须一起做**：480M 仍低于 web 约 800 MB 的工作集，只调 high 仍会换出 ~200 MB；zram 让这部分换出变成内存内压缩。

## 7. 如果基线已经变了（重做补丁）

1. 从线上重新取 `bot/statistics_portal.py`；
2. 对照 `statistics_portal.py.base` → `.new` 的差异，只有三处：
   - 常量区加 `PRECOMPUTED_ROSTER_SECONDS` 和 `_trim_heap()`；
   - `_roster_reply` 开头加 `_precomputed_roster()` 分支；
   - `_cached_query_reply_locked` 的 `result=` 后加 `_trim_heap()` 调用。
3. 用新文件更新 `03-apply.py` 的 `BASE_SHA` 和锚点，重新跑本包 §4.C 的 dry-run。

## 8. 风险与边界

- 重启 `ygo-web` 会清空进程内缓存，首次请求可能走回退计算；挑无查询窗口。
- 预计算进程每 4 分钟占 30–90 秒 CPU（nice 10），总量与现在 web 自算相近或略高；录制高峰若明显受影响，把 timer 改成 6 分钟并把窗口放到 1200 秒。
- 预计算进程也开 30–90 秒读事务，WAL 增长与现状同量级；仍要看 WAL < 512 MB 告警。
- `OOMScoreAdjust=-500` 让内核优先杀 web 而不是 collector；这是保护录制，不是保证 web 不被杀。
- 本包不解决 card-report / matrix 的锁与内存（各 5–35 秒），也不动带宽。

## 9. 未包含

- `/query-api` 排队、keep-alive、缓存头；
- 带宽（3 Mbps 峰值才 10.9%，不需要）；
- QQ / NapCat（观察期）；
- 统计口径、取源、算法（`LOGIC_FREEZE.md`）。
