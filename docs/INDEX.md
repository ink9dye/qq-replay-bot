# 文档索引（唯一导航入口）

> 更新时间：2026-11-11 10:10。**新对话先读本页**，再按顺序读核心文档；历史报告只作证据，不作为现状依据。

## 0. 快速入口

| 项 | 值 |
|---|---|
| 线上服务器 | `101.201.108.188`（Ubuntu 24.04，2C2G） |
| 代码目录 | `/opt/ygobot`（手工 scp 部署，**不是 git 仓库**） |
| 公网入口 | `http://101.201.108.188:8787`（nginx → 分享本体 8789） |
| 统计后台 | `127.0.0.1:8788`（`ygo-web`） |
| 服务 | `ygo-collector`、`ygo-web`、`ygo-qq`、`ygo-replay-api`、NapCat（Docker） |
| 健康检查 | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/health` |
| 交接入口 | [`PROJECT_HANDOFF.md`](../PROJECT_HANDOFF.md) 顶部摘要 + 最新条目 |

- [统一 API 契约](QUERY_API.md)：查询/录像/订阅/绑定/换备/卡牌统计完整对接说明；安卓旧文档保留为跳转入口。

- [安卓 API 补齐上线收尾](WRAPUP_QUERY_API_ANDROID_20261112.md)：部署、真实接口验收、版本及回滚。

## 1. 新对话必读顺序

1. 本页（了解文档全貌）；
2. [`readme.md`](../readme.md)（项目定位、当前状态、常用命令）；
3. [`PROJECT_HANDOFF.md`](../PROJECT_HANDOFF.md) 顶部摘要与最新 1–2 条（当前线上状态、最近改动、遗留）；
4. 按任务读核心文档：架构 → 运维 → 部署 → 功能 → 问题；
5. 规则类任务先读 [`LOGIC_FREEZE.md`](LOGIC_FREEZE.md)（冻结清单）与 [`QQ_OBSERVATION_PERIOD_RULES.md`](QQ_OBSERVATION_PERIOD_RULES.md)（QQ 观察期）。

## 2. 核心文档（长期维护）

| 文档 | 用途 |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 组件、端口、数据流、数据库表、统计口径、外部依赖 |
| [`OPERATIONS.md`](OPERATIONS.md) | 日常体检、日志、备份恢复、告警、故障处置、扩容/清理（含 §11 Side/预热、§12 页面提速运维） |
| [`DEPLOY.md`](DEPLOY.md) | 改代码流程、文件→服务映射、配置/密钥、回滚、常见坑 |
| [`FEATURES.md`](FEATURES.md) | QQ 命令、网页路由、链接权限、卡图卡名、推送 |
| [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) | 当前问题、限制、已修复清单 |
| [`LOGIC_FREEZE.md`](LOGIC_FREEZE.md) | **冻结清单**：取源/卡图/构筑推断/Side/命名等不可擅改项 |

## 3. 当前专题（按需查阅）

| 文档 | 内容 |
|---|---|
| [`QUERY_API.md`](QUERY_API.md) | 服务端总接口：`/query-api`、`/replay-api`、`/statistics/api/recordings` |
| [`REPLAY_LIBRARY_20261106.md`](REPLAY_LIBRARY_20261106.md) | 录像库检索/整场下载、限速、客户端 v1.1–v1.5 |
| [`WRAPUP_REPLAY_DECK_NAMES_20261107.md`](WRAPUP_REPLAY_DECK_NAMES_20261107.md) | **回放库卡组名一致性收尾**：第1局口径、索引实时刷新、ZIP 目录带卡组、部署/回滚 |
| [`WRAPUP_REPLAY_429_20261110.md`](WRAPUP_REPLAY_429_20261110.md) | **录像批量下载 429 收尾（2026-11-10）**：nginx 独立 20r/s 限流、客户端重试/节流、验证与回滚 |
| [`SIDE_ANALYSIS_20261106.md`](SIDE_ANALYSIS_20261106.md) | Side 换入换出分析：入口、口径、算法候选、验收 |
| [`WRAPUP_WEB_LATENCY_20261107.md`](WRAPUP_WEB_LATENCY_20261107.md) | **网页耗时优化全量记录**：措施、实测、饼图最终口径、卡图管线、回滚 |
| [`WRAPUP_PIE_LEGEND_20261107.md`](WRAPUP_PIE_LEGEND_20261107.md) | **前百饼图/图例收尾（2026-11-07）**：网页逐项对齐、网站生成保存饼图+图例、机器人取图、验证与回滚 |
| [`STATISTICS_QUERY_OPTIMIZATION_PLAN_20261106.md`](STATISTICS_QUERY_OPTIMIZATION_PLAN_20261106.md) | 查询链路优化方案（P1b/P2/P3） |
| [`STATISTICS_QUERY_FINAL_PLAN_20261106.md`](STATISTICS_QUERY_FINAL_PLAN_20261106.md) | 查询优化最终执行计划与结论 |
| [`WEB_PERFORMANCE_NIGHT_20261106.md`](WEB_PERFORMANCE_NIGHT_20261106.md) | 2026-11-06 夜间网页性能专项（首屏/缓存/CPU 隔离） |
| [`QUERY_PERF_DESIGN_20261106.md`](QUERY_PERF_DESIGN_20261106.md) / [`QUERY_PERF_PATCH_PLAN_20261106.md`](QUERY_PERF_PATCH_PLAN_20261106.md) | 查询性能设计/补丁计划 |
| [`ALERTING_AND_WAL_20261106.md`](ALERTING_AND_WAL_20261106.md) | 告警重构（PSI/重启/OOM/WAL/磁盘）、WAL 截断结论、**2026-11-08 降噪 v2**（去抖/恢复滞回/单日报/可读文案） |
| [`BANDWIDTH_ACCESS_ANALYSIS_20261106.md`](BANDWIDTH_ACCESS_ANALYSIS_20261106.md) | 带宽/访问分析（3M 小水管优化依据） |
| [`BANDWIDTH_P0_NGINX_GZIP_20261106.md`](BANDWIDTH_P0_NGINX_GZIP_20261106.md) / [`BANDWIDTH_ANDROID_PATCH_20261106.md`](BANDWIDTH_ANDROID_PATCH_20261106.md) | nginx gzip / 安卓端带宽补丁 |
| [`STRESS_ANDROID_20261107.md`](STRESS_ANDROID_20261107.md) | **安卓端容量上限与带宽结论（2026-11-07）**：查询 1–2 人、观战 30–50 台、带宽峰值仅 2%；瓶颈是内存与全局计算锁；含分级压测方案 |
| [`STRESS_RESULT_20261107.md`](STRESS_RESULT_20261107.md) | **受控压测结果（22:26–22:33）**：1/2/4 用户，4 用户 0.94 Mbps、卡牌占 92%、p95 8 s；内存/CPU 未到瓶颈，卡牌 608 KB 是带宽大头 |
| [`CARD_IMAGE_AND_DEVICE_DOWNLOAD_20261106.md`](CARD_IMAGE_AND_DEVICE_DOWNLOAD_20261106.md) | 卡图与设备下载（补充；卡图现行逻辑见 WRAPUP §10） |
| [`CARD_IMAGE_SUPERPRE_20261109.md`](CARD_IMAGE_SUPERPRE_20261109.md) | **卡图串卡（2026-11-09）**：超先行同号图 momobako 会串卡；只认 super-pre；改动/数据修复/验证/回滚 |
| [`WRAPUP_CARD_CODE_IDENTITY_20261109.md`](WRAPUP_CARD_CODE_IDENTITY_20261109.md) | **卡号身份（2026-11-09）**：同名多号（先行号 1013xxxxx / 异画）导致「使用量/单卡流向」全 0；空号回落 + `card-equivalence.json` 全站合并（478 组 / 1102 个卡号）、影响、验证、回滚、维护 |
| [`WRAPUP_CARD_USAGE_GAME1_20261111.md`](WRAPUP_CARD_USAGE_GAME1_20261111.md) | **使用量/单卡流向 口径收尾（2026-11-11）**：使用量曾取「最近一局」把换备当主卡（圣枪 8 人 vs 2 人）；改为每人有确证首局的最近一场，并用「第 2/3 局净换入」作备牌证据；**没有局次信息的录像全局忽略**（6.4% 视角）；单卡流向定稿为一张三栏紧凑图（≥3 人才画条形）+ 文字补小卡组；含最终数字、上线哈希、回滚、影响面、边界 |
| [`QQ_OBSERVATION_PERIOD_RULES.md`](QQ_OBSERVATION_PERIOD_RULES.md) | QQ 账号观察期纪律、掉线 SOP、换号判据 |
| [`QQ_SEND_GUARD_AND_LOGIN_EVIDENCE_PLAN_20261106.md`](QQ_SEND_GUARD_AND_LOGIN_EVIDENCE_PLAN_20261106.md) | 发送守卫与登录证据计划 |
| [`QQ_PROBE_DEPLOY_20261107.md`](QQ_PROBE_DEPLOY_20261107.md) | 自动探测恢复的部署与回滚（2026-11-07） |
| [`WRAPUP_DAILY_CARD_PROBABILITY_20261107.md`](WRAPUP_DAILY_CARD_PROBABILITY_20261107.md) | **每日一卡 + 概率运算**：口径、验证、部署（已装未重启）、回滚、重启后验收 |
| [`WRAPUP_CARD_REPORT_PRECOMPUTE_20261108.md`](WRAPUP_CARD_REPORT_PRECOMPUTE_20261108.md) | **卡片统计/构筑详情预计算上线**：实测、改动、部署记录、验收（8.7 ms / 5.7 ms / 20.2 ms）、回滚 |
| [`CARD_MATCHUPS_20261109.md`](CARD_MATCHUPS_20261109.md) | **单卡胜率 + 对前百卡组 Top5（2026-11-09）**：双方前100、使用后口径、网页/QQ 实现、性能、验证、部署与回滚 |
| [`WRAPUP_WEB_MEMORY_SIDE_INFERENCE_20261109.md`](WRAPUP_WEB_MEMORY_SIDE_INFERENCE_20261109.md) | **web RSS 60→480 MB 根因**：进程内 side inference 整份加载 171 MB 缓存；已移出、TTL/重算频率调整、验收与回滚 |
| [`WRAPUP_MATRIX_PRECOMPUTE_20261109.md`](WRAPUP_MATRIX_PRECOMPUTE_20261109.md) | **卡组胜率提速**：关 series 标注（59→8.1 s）+ 默认 200/200 预计算（首次打开 25–45 ms）、验收与回滚 |
| [`WRAPUP_QQ_BIND_SIDE_PLAYER_20261109.md`](WRAPUP_QQ_BIND_SIDE_PLAYER_20261109.md) | **绑定MC/订阅空格/换备/玩家资料 + 最近5场 Side 收尾（2026-11-09）**：口径、版本、部署、验证、回滚、GitHub；**§8 = 换备先/后攻改按该换边局座位**（13:52 上线，`339a600`/`19fab5f`）；**§9 = 换备只看对手前200**（14:03 上线）＋ 名次快照 14 天（14:10 换备、14:22 全站默认） |
| `WATCH_DEVICE_TOKENS.md`（见仓库 `docs/`，含设备令牌，谨慎分发）/ [`WATCH_CAPACITY_20261105.md`](WATCH_CAPACITY_20261105.md) / [`SMALL_WORLD.md`](SMALL_WORLD.md) | 设备令牌、观战容量、小世界检索 |
| [`SERVICE_BOUNDARIES_20261105.md`](SERVICE_BOUNDARIES_20261105.md) | 服务边界与职责划分 |

## 4. 规则与流程

| 文档 | 要点 |
|---|---|
| [`AGENTS.md`](../AGENTS.md) | 新对话 / 协作者须知：接手顺序、硬性规则、部署与协作约定 |
| [`LOGIC_FREEZE.md`](LOGIC_FREEZE.md) | 冻结项：取源、卡图、构筑推断、Side 筛选、命名算法/参数 |
| [`DECK_BUILD_SIDE.md`](DECK_BUILD_SIDE.md) | 构筑 Side 口径与展示约定 |
| [`WEB_CHECKLIST.md`](WEB_CHECKLIST.md) | 网页改动验收清单（一次只改一个功能） |
| [`QQ_OBSERVATION_PERIOD_RULES.md`](QQ_OBSERVATION_PERIOD_RULES.md) | 观察期禁止项与处置流程 |

## 5. 历史报告（只作证据，不代表现状）

> 这些是某次任务的一次性记录；需要追溯“为什么当时这么做”时再读。**不要把它们当当前状态。**

- 收尾类：`WRAPUP_20261106.md`、`WRAPUP_REPLAY_CLIENT_20261106.md`；
- 修复/事故类：`INCIDENT_2026-11-03_MEMORY.md`、`QUERY_LATENCY_DIAGNOSIS_20261106.md`、`QUERY_REPLAY_AUTH_DOWNLOAD_20261106.md`、`STATISTICS_PERFORMANCE_20261105.md`；
- 迁移/评估类：`QUERY_API_DEPLOYMENT_20261105.md`、`QUERY_MIGRATION_MERGE_RECOMMENDATIONS_20261105.md`、`TOKEN_TASK_CACHE_CANDIDATE_20261104.md`、`DEEPSEEK_SESSION_IDENTITY_HANDOFF_20261104.md`；
- 安卓取证类：`ANDROID_REPLAY_EVIDENCE_REPAIR_PLAN_20261105.md`、`ANDROID_SERVER_EVIDENCE_FIX_20261105.md`；
- 旧版说明：`HISTORY_README_20261102.md`（2026-11-02 的 readme 原文存档）。

## 6. 目录约定

| 目录 | 用途 | 是否长期保留 |
|---|---|---|
| `docs/` | 长期文档与专题报告 | 是 |
| `PROJECT_HANDOFF.md` | 交接流水（顶部最新） | 是 |
| `workflow-results/` | 一次性验证产物/截图/证据 | 按需清理 |
| `archive/` | 已废弃实现/旧饼图等存档 | 是（只读） |
| `.tmp-ops/` | 临时工作树/交付分支（含 `delivery-query-perf-20261106`） | 交付后清理 |
| `/opt/ygobot/data/config-backups/` | 线上文件备份（部署前） | 是 |

## 7. 文档维护约定

1. **新报告**放 `docs/`，文件名带日期后缀（`_YYYYMMDD`）；一次性证据放 `workflow-results/<主题>-<日期>/`；
2. 改完代码/配置，更新 `PROJECT_HANDOFF.md` **顶部摘要 + 最新条目**（含版本 sha 前 16、验证、回滚）；
3. 新增文档后**在本页加一行**（分到对应章节）；不要改历史报告正文；
4. 同一事实只保留一个“最新版本”，历史用链接引用，不复制多份；
5. 文档里避免写死过期路径/进程号；以 handoff 顶部和实际 `sha256sum` 为准。

- [卡片模糊查询与合并资料收尾](WRAPUP_CARD_PROFILE_20261111.md)

- [QQ单卡胜率先后攻拆分收尾](WRAPUP_CARD_WINRATE_ORDER_20261111.md)：逐小局分组、真实库验证、部署与回滚。

- [前200内战最高观战优先级收尾](WRAPUP_WATCH_TOP200_PRIORITY_20261112.md)：分档规则、33项验证、部署SHA、重启验收与回滚。

- [QQ管理员代开通与保密群临时私聊修复](WRAPUP_QQ_DELEGATION_TEMP_PRIVATE_20261112.md)：权限边界、18项验证、部署与回滚。

- [Pie intersection fix (2026-11-12)](WRAPUP_PIE_INTERSECTIONS_20261112.md): deployment, verification and rollback.
