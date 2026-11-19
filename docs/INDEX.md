# 文档索引（唯一导航入口）

- [前百内战与 QQ、网页复用核对](wrapup/WRAPUP_INNER100_PARITY_20261119.md) — 已部署，附入口核对与回滚。

- [QQ 与网页前 200 内战统一](wrapup/WRAPUP_INNER_COUNT_20261119.md) — 共同资格、双计数、部署与回滚。

- [卡组筛选统一与回归复查](wrapup/WRAPUP_DECK_FILTERS_20261119.md) — 已部署，验证、版本与回滚。

- [QQ 卡片资料减负](wrapup/WRAPUP_QQ_PROFILE_REUSE_20261118.md) — 2026-11-19 已部署，共用前百快照，验证与回滚。

- [内战资格和网页构筑减负](wrapup/WRAPUP_INNER_PAIRS_BUILD_20261118.md)：对手配对复用、构筑单页提速、验证与回滚。


- [卡报后台积压与重复计算修复](wrapup/WRAPUP_CARD_REPORT_QUEUE_20261118.md) — 生产部署、浏览器冒烟、待验项与回滚。


- [QQ单卡胜率使用与起手双结果](wrapup/WRAPUP_CARD_WINRATE_OPENING_20261117.md)：双方前200，复用数据分两条回复；验证、部署与回滚。

- [28天保留与QQ单卡胜率前200收尾](wrapup/WRAPUP_RETENTION28_CARD200_20261117.md)：已部署，迁移32,287条；使用量/流向/卡组仍前100，验证及回滚。

- [对局胜率按最新构筑修复](wrapup/WRAPUP_GAME_WINRATE_BUILD_20261117.md)：2026-11-17已部署；与当前构筑录像同源，23局验收、版本与回滚。

> 更新时间：2026-11-15 16:50。**新对话先读本页**，再按顺序读核心文档；历史报告只作证据，不作为现状依据。
>
> **本版结构（2026-11-13 已按分类物理分层）**：`docs/` 顶层只留本页，其余 75 份文档 + 1 份清单按五类归入子目录，
> 每个目录内有一份 `README.md` 做该类导航（清单 + 一句话结论 + 状态）：
> [`core/`](core/README.md)（11 份：结论与规则）· [`topics/`](topics/README.md)（24 份：现行专题）·
> [`wrapup/`](wrapup/README.md)（26 份：历史收尾）· [`incident/`](incident/README.md)（1 份：事故复盘）·
> [`archive/`](archive/README.md)（19 份 + 1 表：一次性方案）。
> 搬迁时已同步改写全库 262 处链接；**新文档直接放进对应分类目录**，并在该目录 README 补一行。

## 0. 一分钟入口

- [单卡使用量漏人修复收尾](wrapup/WRAPUP_CARD_USAGE_SESSION_20261115.md)：已部署并上传master；同批96→100，同场比较与回滚。

待办总入口：[QQ观战统计系统_未来修改计划.md](../QQ观战统计系统_未来修改计划.md)（当前待办总表、历史遗留待核验、已完成项；专题保留证据）。

| 你需要 | 读这份 |
|---|---|
| **第一次接触这个项目** | [`INTRODUCTION.md`](core/INTRODUCTION.md) —— 全貌、数据流、口径、限制、上手路径（约 10 分钟） |
| **想知道某个功能内部怎么算的** | [`manual/README.md`](core/manual/README.md) —— 功能详解手册 11 章：采集/解析/构筑/命名/Side/统计/网站/QQ/录像库/资源/查询接口 |
| **动手改任何东西之前** | [`LESSONS.md`](core/LESSONS.md) —— 43 条「现象→根因→规矩→出处」+ 一页速查 |
| **这事以前做过没、当时怎么定的** | [`WRAPUP_LEDGER.md`](wrapup/WRAPUP_LEDGER.md) —— 23 篇收尾的一句话结论与状态，含 13 项未完成 |
| **现在线上是什么状态** | [`PROJECT_HANDOFF.md`](../PROJECT_HANDOFF.md) 顶部摘要 + 最新 1–2 条 |
| **有个已知 bug 在立案** | [`BOT_FREEZE_TODO_20261110.md`](topics/BOT_FREEZE_TODO_20261110.md) —— 机器人偶尔卡死 |

## 1. 五个分类导航

| 分类 | 导航页 | 装什么 | 什么时候看 |
|---|---|---|---|
| 核心 | [`core/README.md`](core/README.md) | 11 份：架构/运维/部署/功能/问题/冻结/口径/清单/观察期/导读/教训 + [`manual/`](core/manual/README.md) 功能手册 11 章 | 接手、改规则、动网页、查某个功能怎么算 |
| 现行专题 | [`topics/README.md`](topics/README.md) | 24 份：接口、录像库、Side、卡图、卡号身份、性能观测、容量、带宽 | 要改某个具体功能 |
| 收尾 | [`wrapup/README.md`](wrapup/README.md) | 26 份：每轮任务的完整收尾（改了什么/怎么验证/怎么回滚/留下什么坑） | 查历史决策与数字来历 |
| 事故 | [`incident/README.md`](incident/README.md) | 1 份：2026-11-03 内存耗尽整机卡死复盘 | 出故障、判断会不会再炸 |
| 归档 | [`archive/README.md`](archive/README.md) | 19 份 + 1 表：一次性方案、部署记录、候选补丁、迁移建议 | 追溯「为什么当时这么做」 |

## 2. 新对话必读顺序

> **第一次接触本项目**：先读 [`INTRODUCTION.md`](core/INTRODUCTION.md)（全貌导读），再按下面顺序走。
> 已经熟悉项目、只接一个具体任务的对话，可直接从第 3 步开始。
> **动手改任何东西之前**：扫一遍 [`LESSONS.md`](core/LESSONS.md) 里对应主题（部署/口径/性能/QQ）。

1. 本页（了解文档全貌与分类）；
2. [`readme.md`](../readme.md)（项目定位、当前状态、常用命令）；
3. [`PROJECT_HANDOFF.md`](../PROJECT_HANDOFF.md) 顶部摘要与最新 1–2 条（当前线上状态、最近改动、遗留）；
4. 按任务读 [`core/README.md`](core/README.md) 里的架构 → 运维 → 部署 → 功能 → 问题；
5. 规则类任务先读 [`LOGIC_FREEZE.md`](core/LOGIC_FREEZE.md)（冻结清单）与 [`QQ_OBSERVATION_PERIOD_RULES.md`](core/QQ_OBSERVATION_PERIOD_RULES.md)（QQ 观察期）；
6. 要改具体功能，先到 [`topics/README.md`](topics/README.md) 找该主题的现行说明。

## 3. 运行环境速查

| 项 | 值 |
|---|---|
| 线上服务器 | `101.201.108.188`（Ubuntu 24.04，2C2G） |
| 代码目录 | `/opt/ygobot`（手工 scp 部署，**不是 git 仓库**） |
| 公网入口 | `http://101.201.108.188:8787`（nginx → 分享本体 8789） |
| 统计后台 | `127.0.0.1:8788`（`ygo-web`） |
| 服务 | `ygo-collector`、`ygo-web`、`ygo-qq`、`ygo-replay-api`、NapCat（Docker） |
| 健康检查 | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/health` |
| 交接入口 | [`PROJECT_HANDOFF.md`](../PROJECT_HANDOFF.md) 顶部摘要 + 最新条目 |

## 4. 文档之外的入口

| 位置 | 内容 |
|---|---|
| [`AGENTS.md`](../AGENTS.md) | 新对话 / 协作者须知：接手顺序、硬性规则、部署与协作约定 |
| [`../price-watch/docs/reservations.md`](../price-watch/docs/reservations.md) | 本地独立预定工具：桌面管理、Windows 登录自启、到价邮件 |
| [`../workflow-results/bug-audit-20261111/REPORT.md`](../workflow-results/bug-audit-20261111/REPORT.md) | 2026-11-11 线上基准测试报告：两处 P2 隔离复现、证据与测试边界 |
| `WATCH_DEVICE_TOKENS.md` | 设备令牌（含凭据，**刻意不落本地**；需要时向维护者索取，谨慎分发） |

## 5. 文档维护约定

- [卡图串卡治本与台账巡检补图](topics/CARD_IMAGE_NAMING_SAFETY_20261117.md)：2026-11-17已部署（web）；写入只按卡密、猜号降级为读兜底、巡检脚本首跑补全 2 张。
- [卡图统一入口与保底](topics/CARD_IMAGE_PLACEHOLDER_20261116.md)：2026-11-16已部署（web）；`resolve()`、占位图、冷却与台账，QQ 侧待自然重启。
- [单卡图分母与最新比赛首局缺失待办](topics/CARD_USAGE_SESSION_TODO_20261115.md)：分母修复已授权未实施；4名玩家最新场仅有第2/3局的原因待排查。

1. **新报告**放对应分类目录（`core/` `topics/` `wrapup/` `incident/` `archive/`），文件名带日期后缀（`_YYYYMMDD`）；
   一次性证据放 `workflow-results/<主题>-<日期>/`；
2. 改完代码/配置，更新 `PROJECT_HANDOFF.md` **顶部摘要 + 最新条目**（含版本 sha 前 16、验证、回滚）；
3. 新增文档后**在本页与所属分类 README 各加一行**；不要改历史报告正文；
4. 同一事实只保留一个「最新版本」，历史用链接引用，不复制多份；
5. 文档里避免写死过期路径/进程号；以 handoff 顶部和实际 `sha256sum` 为准；
6. **`docs/` 顶层只放 `INDEX.md`**；新文档写进对应分类目录，并在该目录 README 与本页各补一行。

## 6. 目录约定

| 目录 | 用途 | 是否长期保留 |
|---|---|---|
| `docs/` | 只剩 `INDEX.md`（总导航） | 是 |
| `docs/core` | 结论与规则：架构/运维/部署/功能/问题/冻结/口径/清单/观察期/导读/教训 | 是 |
| `docs/topics` | 现行专题：接口、录像、Side、卡图、身份、性能、容量、带宽 | 是 |
| `docs/wrapup` | 历史收尾：每轮任务的完整记录（含台账） | 是 |
| `docs/incident` | 事故复盘 | 是 |
| `docs/archive` | 一次性方案、部署记录、候选补丁、迁移建议（只读） | 是 |
| `PROJECT_HANDOFF.md` | 交接流水（顶部最新） | 是 |
| `workflow-results/` | 一次性验证产物/截图/证据 | 按需清理 |
| `archive/` | 已废弃实现/旧饼图等存档 | 是（只读） |
| `.tmp-ops/` | 临时工作树/交付分支（含 `delivery-query-perf-20261106`） | 交付后清理 |
| `/opt/ygobot/data/config-backups/` | 线上文件备份（部署前） | 是 |

- [Side连接与查卡阻塞修复收尾](wrapup/WRAPUP_SIDE_CARD_RESOURCES_20261114.md)：2026-11-14已部署，验证、版本与回滚。

- [录像分阶段观测与轮转读取](topics/WEB_PERF_PHASES_20261114.md)：已上线；补读logrotate、阶段记录、覆盖边界和真实证据。

- [单卡胜率饼图人数Top5收尾](wrapup/WRAPUP_CARD_WINRATE_POPULAR_20261114.md)：2026-11-14已部署；验证、影响与回滚。

- [名册页玩家搜索“假空结果”](topics/PLAYER_SEARCH_EMPTY_20261115.md)：2026-11-15立案待办；根因、接口实测与最小修复方案，未改代码。

- [QQ前百饼图代表卡纯蓝修复](wrapup/WRAPUP_PIE_REP_IMAGE_20261115.md)：2026-11-15已部署并推送；根因、三层修复、验证与回滚。
- [组合改名不生效的治本](wrapup/WRAPUP_SERIES_AUTO_REFRESH_20261115.md)：2026-11-15已部署；根因、按批自愈、验证与回滚。
- [组合改名的两条速度](wrapup/WRAPUP_RENAME_TIERED_20261115.md)：2026-11-15已部署；前端立刻改、历史进慢速队列，验证与回滚。

- [功能详解手册](core/manual/README.md)：2026-11-15 建立并同日复核；11 章逐功能写「流程→参数→失败模式→核实记录」，覆盖采集、解析、构筑命名、Side、统计口径、网站、QQ、录像库、后台任务、数据模型、查询接口 `/query-api`。

- [QQ命令分阶段耗时观测](topics/QQ_TIMING_OBSERVATION_20261115.md)：本地候选，阶段口径/汇总/测试/上线边界。

- [统计链接失败与恢复](topics/STATS_LINK_FAILURE_20261116.md)：已部署，用户确认恢复；短连接重试、脱敏诊断与回滚。
