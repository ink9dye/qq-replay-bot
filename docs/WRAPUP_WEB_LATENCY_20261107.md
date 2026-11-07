# 网页耗时优化全量记录（2026-11-06 晚 – 11-07 上午）

本文按“措施 → 做法 → 实测 → 状态”汇总这段时间对网页（`/statistics` 系列页面）做过的全部耗时优化，供后续排查和复用。**未上线或已回滚的尝试也如实列出。**

## 0. 目标与环境

- 服务器：Ubuntu 24.04，2 vCPU / 1.7GB，web 进程内存上限 500MB（`MemoryHigh=420M`）。
- 约束：不卡顿、不影响 QQ/采集；共享文件（UI 分支的 `statistics_portal.html`、`statistics_dashboard.py`、`statistics_portal.py`）不擅自覆盖。
- 观测：`/opt/ygobot/data/logs/statistics-query-timings.jsonl`（每次后台计算耗时）、`/health`、`memory.events`。

## 1. 措施清单

### 1.1 资源隔离（系统层）
- web：`CPUWeight=200`；collector：`CPUQuota=120%`（未重启 collector，仅设属性）。
- 效果：load 2.3–3.5 → 0.9–1.5；白天峰值不再把 web 挤到卡死。
- 文件：`/etc/systemd/system/ygo-web.service.d/cpu-budget.conf`。

### 1.2 请求缓存层（QueryRefresh + last-success）
- 单 worker 串行计算；结果写 `last-success` 磁盘缓存（跨重启可读 30 分钟）。
- 缓存预算：`MAX_KEYS 16→256`，`MAX_BYTES 8MB→64MB`（避免构建/名册结果被挤掉）。
- 失效：`snapshot_version`（DB 版本）变化即失效；前台命中直接返回，带 `X-Statistics-State: fresh/stale/refreshing`。
- 效果：warm 命中 5–50ms；用户感知的“卡”主要来自首次/换参数。

### 1.3 前百卡组图（deck images）持久化
- 原：每次 roster 计算对 `stat_build_cards` 全表聚合（8.8–15s）。
- 改：按卡组缓存代表卡 30 分钟，落盘 `data/cache/deck-images.json`（ygo 可写），启动加载。
- 效果：roster 冷算 22–50s → 5–8s；命中 6–9ms。

### 1.4 构筑页单玩家化
- 原：每个构筑页都算完整 100 人名册（2–18s）。
- 改：`population(player=...)` 只算该玩家视角（资格/展示规则不变）；`deck-detail` 走 QueryRefresh 持久化。
- 效果：构筑页 3–18s → 0.45–2.3s。

### 1.5 构筑页绕开全局计算锁
- 问题：`deck-detail` 排在全局 `_query_compute_lock` 后面，被 roster/matrix 的 10–60s 计算堵到 8 秒超时（503）。
- 改：`deck-detail` 单独走 `_query_reply`（不抢全局锁），等待 4 秒。
- 效果：冷构筑 0.45–2.3s 直接 200，不再 503。

### 1.6 矩阵页瘦身
- 页面版请求 `samples=0`，不再传逐局样本；载荷 460KB → 94KB（100/100）。
- 预热：`prewarm` 轮转加入 matrix 100/100 与 200/200；构筑每 5s 一个。
- 效果：矩阵命中 0.03s；200/200 冷算 25s（旧）→ 6.6s（系列缓存后）。

### 1.7 静态资源缓存
- `/statistics/assets/*` 由 `no-store` 改为 `private, max-age=3600`（页面本身仍 `no-store`）。
- 效果：浏览器不再每次重下 JS/CSS。

### 1.8 系列标注缓存（本阶段重点）
- 问题：`eligible_views → card_series.annotate` 对每个构筑逐卡计算系列标签，单次 10–25s，roster/matrix/录像/卡片统计都要付。
- 改法（最终线上版 `card_series.py 7a09fe28`）：
  1. 按 `(catalog revision, build, 分类来源, 主/额外完整度, series_status)` 缓存标注结果；
  2. **落盘** `data/cache/build-series.json`（紧凑元组格式，约 3.9–12.7MB，按 revision 剪枝，上限 8000 条）；
  3. 分块计算（每块 250 个构筑），逐块写回行并释放 cards 字典，峰值内存有界；
  4. 全命中时跳过 `stat_build_series`/`stat_build_cards` 查询与逐卡循环（快路径）。
- 效果（同库同时刻，输出逐项一致）：
  - roster：26.6s → **10.7s**（进程内二次 1.2–4.3s）；
  - 矩阵 200/200：58.6s → **6.6s**；
  - 录像 100/100：6.8s → **2.7s**；
  - 卡片统计默认 200/200：18s → **5.7s**；
  - 峰值内存：fresh 进程 96–119MB；线上 RSS 196–256MB，无 OOM。
- **失败/回滚记录（重要）**：
  - 第一版“嵌套 dict + 全量落盘”上线后，`memory.high` 10 分钟触发 3000+ 次、RSS 440MB、一次 roster 计算 112s → 回滚；
  - 第二版改紧凑元组仍偏大（12.7MB 文件）→ 再回滚；
  - 最终版用“字符串拼接 + 分块 + 8000 条上限”，RSS 稳定在 196–256MB，`memory.high` 仍偶发（2572 次累计）但无 OOM，roster 10.7s。

### 1.9 存活检查批量快路径（已改，未上线）
- 现状：线上 `top100.eligible_views` 逐文件 `safe_replay_file`（每次 2–3s）。
- 改法：恢复 `replay_alive.alive_map`（每父目录列一次，等价判定）。
- 实测：卡片统计 14.7→13.9s（**无明显收益**），未部署，避免无谓改动。

### 1.10 其它
- WAL 128MB、快照 DDL（P1b）；
- 查询计时日志（`statistics-query-timings.jsonl`）用于归因；
- 回滚脚本与备份目录（见第 4 节）。

## 2. 当前线上版本（2026-11-07 09:45）

| 文件 | sha256 前16 | 说明 |
|---|---|---|
| `statistics_portal.py` | `e8d77d63` | UI 分支 19:56 版 + 资源缓存/绕锁 |
| `statistics_dashboard.py` | `d85c46df` | UI 分支版本（未改） |
| `statistics_portal.html` | `5f3f642d` | UI 分支版本（未改） |
| `statistics_matrix.py` | `26de2d49` | 我的瘦身版 |
| `statistics_matrix.html` | `8b1d5146` | samples=0 |
| `card_series.py` | `7a09fe28` | 系列缓存（紧凑 + 分块） |
| `top100_pie.py` | `ad6e752d` | QQ 饼图 payload 缓存（待 QQ 重启生效） |

## 3. 实测汇总（2026-11-07 09:40 前后）

| 场景 | 现在 | 优化前 |
|---|---:|---:|
| `/statistics` 页面 | 5–6ms | 5–6ms |
| roster API（命中） | 6–9ms | 6–9ms |
| roster 后台计算 | 10.7s | 26.6s |
| 矩阵 100/100 / 200/200 计算 | 6.6s | 25 / 58.6s |
| 构筑页（冷） | 0.45–2.3s | 3–18s（常 503） |
| 录像 100/100 计算 | 2.7s | 6.8s |
| 卡片统计 200/200 计算 | 5.7s | 18s |
| web RSS | 196–256MB | 200MB |
| load | 0.8–1.4 | 2.3–3.5 |

## 4. 回滚入口

- 系统：`/root/ygo-web-cpu-budget.conf.bak-*`；`systemctl set-property ygo-collector CPUQuota=infinity`。
- 系列缓存：`/root/last-series-bounded-backup`（回滚到 6e5fe2b6 仅内存版）、`/root/build-series.json.disabled-*`（删缓存）。
- 门户：`/root/last-portal-merge-backup`、`/root/statistics_portal.before-wait.py`。
- 饼图（QQ）：`/root/top100_pie.before-cache.py`。

## 4.5 饼图中环交集 + 资格快照单槽缓存（2026-11-07 上午）

- **饼图中环**：statistics_portal.html 的 
enderPie() 原本只画内环（主大类）与外环（具体卡组），pie.middle 只用于图例和取图。已补一段“中环交集”绘制：按所属大类的角度，把 middle 中**所有带 extras 的交集**（不再限制人数）画成 0.555R–0.605R 的细弧（颜色取 mixed/color），并推入标签列表走原有引导线，文本为「大类+∩交集 人数」（如「混沌∩三幻魔 3人」）。1 人交集的角度约 0.6°，视觉上很细但存在；标签会变多，若嫌拥挤可只给 ≥2 人的交集加标签、弧仍全画。这是纯展示增补，不改统计口径。
- **资格快照单槽缓存**：	op100.eligible_views 增加单槽缓存，键为 (snapshot_version, 30 秒时间桶, 范围 own/opponent, internal_only, player, check_files, annotate_series, quality, series revision)，命中原样返回（views 列表复制一层，避免调用方改写共享 dict）；**只缓存 ≤8000 行的结果**，200/200 宽口径不缓存，避免 40–60MB 级内存。
- **实测**：卡片统计 100/100 冷算由 17.5s 降到 **7.8s/6.3s**（生产 timings 09:54）；复测 200/200 约 10–18s（宽口径不缓存）；web RSS 173MB、memory.events 全 0。
- **回滚**：/root/last-pie-middle-backup（饼图 html + top100 原文件）。

## 4.6 单环饼图：交集相邻排序（2026-11-07 上午）

- 单环饼图（
enderSinglePie）的切片顺序原本按人数降序。现改为**交集优先相邻**：
  - 从 deck_pie.middle 取“两侧都是本图切片、且都不是「其他」”的交集对，按人数作为权重建图；
  - 每个切片的交集数 **>3** 时只保留权重最大的 **2** 个，否则全保留；
  - 从权重最大的边开始建链，两端按权重贪心扩展，其余切片按人数降序追加；交集对因此尽量相邻，相邻对由原有 steps 机制画成边界缺口。
- 当前数据（单环切片：无垢大艺术20/混沌20/三幻魔9/时黑7/盈彩月夜7/烙印6/其他31）有效交集对：混沌∩三幻魔3、混沌∩无垢大艺术1、混沌∩烙印1、时黑∩烙印1；排序结果为“无垢大艺术→混沌→三幻魔→烙印→时黑→盈彩月夜→其他”，混沌与三幻魔、无垢大艺术相邻，烙印与时黑相邻。
- 限制：链式排序无法让一个切片同时与 3 个以上伙伴相邻；不相邻的交集对（如跨越其他切片）不会画缺口。

## 4.7 单环饼图：嵌套小类代表卡按“构筑内卡数最多的字段”（2026-11-07 上午）

- 口径：单环的**大类**=命名组字段；**小类**=大类内 >4% 的**具体卡组**（与旧“内环大类/外环卡组”一致）。
- 问题：小类（嵌套卡组）的代表卡原先由 deck_pie.layers 取“全局覆盖人数最多的字段”的代表卡，例：米底乌斯无垢大艺术狱神耀圣 落到「幻狱神 米底丘利乌斯」（狱神 70488851）。
- 改动：statistics_dashboard.query 按 series_membership 汇总每个卡组名的字段卡数（主卡+额外），传给 deck_pie；layers 取该卡组内**卡数最多且不等于本大类字段**的字段的代表卡（并列按字段名长度）。两个小类若因此取到同一张代表卡，前端沿用既有“相邻同卡图合并”规则只贴一次卡图、不画白线。
- 实测：该卡组 耀圣 133 张 > 狱神 82 > 无垢 64，线上 roster 的 deck_pie.outer.image 与 deck_single 嵌套 band image 均变为 **5559570（耀圣）**。
- 运维注意：roster 结果有 300s 缓存且版本戳不含 Python 代码；部署后需清 data/query-cache/last-success/*.json 并重启（或等缓存过期），否则会继续返回旧图。回滚：/root/last-pie-fieldrep-backup。

## 5. 仍未做（按优先级）

1. **卡片统计**：瓶颈已定位为“32 个分桶重建（最多 8.7s）+ 实时名次核验（约 5s）+ 存活检查（2–3s）”，不是缺索引；下一步是“资格快照单槽缓存（约 40MB）+ 名次短时效磁盘缓存（需确认 120s 语义）”；
2. roster/记录的名称与证据缓存（约 1–2s）；
3. 前端骨架屏/并行请求（UI 分支负责）；
4. gzip/HTTP 压缩（当前载荷已缩小，收益有限）。

## 6. 经验

- 先量化再动手：`timings` 日志 + cProfile 能直接指出 annotate/alive/rank 谁在耗时。
- 共享文件先 diff 再覆盖；本次饼图/侧分析路由被覆盖的教训见 `PROJECT_HANDOFF.md` 事故条目。
- 缓存要按“内存上限”设计：紧凑格式 + 分块 + 条目上限，否则 500MB 上限下必然触发 `memory.high`。


## 9. 最终状态（2026-11-07 10:20）

### 9.1 线上文件

| 文件 | sha256 前16 | 来源 |
|---|---|---|
| `statistics_portal.py` | `e8d77d63` | UI 分支 19:56 版 + 资源缓存/绕锁 |
| `statistics_portal.html` | `906d8f6d` | UI 分支 23:51 版 + 中环交集/相邻排序 |
| `statistics_dashboard.py` | `51e600ae` | UI 分支版 + 每卡组字段卡数 |
| `deck_pie.py` | `420a547f` | 小类代表卡排除大类字段 |
| `card_series.py` | `7a09fe28` | 紧凑字符串缓存 + 分块 annotate |
| `statistics_matrix.py` / `.html` | `26de2d49` / `8b1d5146` | 矩阵瘦身 |
| `top100.py` | `1288a888` | 线上原版（资格快照缓存因内存回滚） |
| `statistics_prewarm.py` | `4c7a2245` | 预热轮转 |
| `top100_pie.py` | `ad6e752d` | QQ 饼图 payload 缓存（待 QQ 重启生效） |

### 9.2 饼图最终口径

- 单环：大类 = 命名组字段；小类 = 大类内 >4% 的具体卡组，嵌套在 0.56–0.94R；
- 小类代表卡 = 该构筑内 **卡数最多且 ≠ 本大类字段** 的字段代表卡（无垢大艺术两个小类都取耀圣 5559570 → 同图合并、无白线）；
- 交集 = 相邻切片边界小缺口 + 图例（用户确认够用），排序优先让有交集切片相邻；单片交集 >3 时只保留权重最大的 2 个相邻；
- 中环交集弧（双环渲染器）保留但单环优先显示。

### 9.3 实测（10:19）

| 项 | 数值 |
|---|---|
| roster 计算 | 10.7s（命中 6–17ms） |
| 矩阵 200/200 | 6.6s |
| 录像 100/100 | 2.7s |
| 卡片统计 200/200 | 5.7–14s（宽口径不缓存） |
| 构筑页 | 0.45–2.3s |
| web RSS | 195MB，`memory.events` 全 0 |
| health | 200（web/qq/collector active） |

### 9.4 回滚入口

- 饼图代表卡：`/root/last-pie-fieldrep2-*`、`/root/last-pie-fieldrep-backup`；
- 饼图渲染：`/root/last-pie-middle-backup`；
- 系列缓存：`/root/last-series-bounded-backup`、`/root/build-series.json.disabled-*`；
- 门户：`/root/last-portal-merge-backup`；
- 缓存：roster 的 `data/query-cache/last-success/*.json` 可删（会触发重算）。

### 9.5 GitHub

`delivery/query-perf-20261106`：`ea25b2b` → `1146c23` → `29e4bf0` → `377045a` → `afbb34d` → `e55ed63` → `43099c7` → `8e86744` → `c8ccabe` → `3c08f07` → `ee62da8` → 最终快照提交。

## 10. 卡图存储与调用逻辑（2026-11-07 补充）

### 10.1 存储位置

| 目录 | 内容 | 说明 |
|---|---|---|
| `ygopro/pics/<code>.jpg|.png` | 全图 | ygopro 本地卡图库；`library_root(data_dir)` 定位；含 `100xxxxxx` 异画编号变体 |
| `data/cache/card-art/<code>.jpg` | 全图缓存 | 后端从 CDN 补下后的缓存 |
| `data/card-thumbs/<code>.webp` | 缩略图 120×165 | 本地生成或从 CDN `!thumb2` 下载 |

### 10.2 查找顺序

- **全图**：`statistics_local_cards.picture(data_dir, code)` → `library_root/pics`（按 `image_id` 映射找本体号/异画号）→ 命中即返回；未命中时**后台**从 `https://cdn.233.momobako.com/ygopro/pics/<code>.jpg` 补齐（线程池，不阻塞请求）。
- **缩略图**：`web_thumb()` → `data/card-thumbs`（同样支持异画映射）→ 无则用本地全图 `card_image_cache.local_full` 派生 webp → 仍无则后台从 `...jpg!thumb2` 下载。
- 其他模块可直接用 `statistics_local_cards.thumb_picture()` 取缩略图路径。

### 10.3 HTTP 路由

| 路由 | 行为 | 缓存头 |
|---|---|---|
| `/statistics/card-image/<code>` | 返回本地全图；本地没有 → 404（文案“本地暂无卡图”） | `public, max-age=86400` |
| `/statistics/card-thumb/<code>` | 返回本地缩略图；本地没有 → 302 到 CDN `!thumb2` | `private, max-age=86400` |
| `/statistics/api/card-info?code=` | 名称/描述等文本，不含图片 | 同上接口缓存 |

### 10.4 前端调用链

- 饼图图例/卡图：`cardImageUrl` → `/statistics/card-thumb/<code>`；
- 构筑页卡格 `cardTile`：本地缩略图 → 失败切 `cdntx.moecube.com` 远程 → 再失败显示“暂无卡图”；
- 构筑页卡片弹窗 `showCard`：远程大图 `cdn.233.momobako.com/...jpg!half` → 失败 → 本地全图 `/statistics/card-image/` → 失败 → 本地缩略图 `/statistics/card-thumb/` → 再失败显示“暂无卡图”（后两级为本次新增）。

### 10.5 为什么会出现“本地暂无卡图”

例：`101307036`（幻之昆虫女王）本地只有缩略图 `data/card-thumbs/101307036.webp`，没有全图；而弹窗原先只请求远程大图，远程无图就显示占位文案。现在弹窗会回退到本地缩略图，所以能正常显示。

### 10.6 维护提示

- 两种图都由后台线程池补齐（`_thumb_pool`、`_schedule`），失败有冷却，不阻塞页面；
- 改了 `statistics_build.js` 后浏览器可能缓存 1 小时（资源头 `max-age=3600`），需要 Ctrl+F5；
- 新增卡图优先放 `ygopro/pics` 或 `data/cache/card-art`，缩略图会自动派生/补下。

