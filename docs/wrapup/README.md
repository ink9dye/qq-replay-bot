# 收尾文档导航（wrapup）

- [前百内战与 QQ、网页复用核对](WRAPUP_INNER100_PARITY_20261119.md) — 已部署，附入口核对与回滚。

- [QQ 与网页前 200 内战统一](WRAPUP_INNER_COUNT_20261119.md) — 共同资格、双计数、部署与回滚。

- [卡组筛选统一与回归复查](WRAPUP_DECK_FILTERS_20261119.md) — 已部署。

- [QQ 卡片资料减负](WRAPUP_QQ_PROFILE_REUSE_20261118.md) — 已部署，记录统计等价对照、上线验证及回滚。

- [内战资格和网页构筑减负](WRAPUP_INNER_PAIRS_BUILD_20261118.md)：对手配对复用、构筑单页提速、验证与回滚。


- [卡报后台积压与重复计算修复](WRAPUP_CARD_REPORT_QUEUE_20261118.md) — 生产部署、浏览器冒烟、待验项与回滚。


- [QQ单卡胜率使用与起手双结果](WRAPUP_CARD_WINRATE_OPENING_20261117.md)：双方前200，复用数据分两条回复；验证、部署与回滚。

- [28天保留与QQ单卡胜率前200收尾](WRAPUP_RETENTION28_CARD200_20261117.md)：已部署，迁移32,287条；使用量/流向/卡组仍前100，验证及回滚。

- [对局胜率按最新构筑修复](WRAPUP_GAME_WINRATE_BUILD_20261117.md)：2026-11-17已部署，与当前构筑录像同源；验证、版本与回滚。

> **本目录就是「历史收尾」层，正文就在本页旁边**（24 份 + 本页）。下表按时间倒序，每条给一句话结论与状态。
> 返回总导航：[`../INDEX.md`](../INDEX.md) ｜ 其他分类：[`../core/`](../core/README.md) · [`../topics/`](../topics/README.md) · [`../incident/`](../incident/README.md) · [`../archive/`](../archive/README.md)

## 什么时候看这一类

两类时刻：① **动手前想查「这事以前做过没、当时怎么定的」**；② **追溯某个数字/口径的来历**。
这里的每篇都是某轮任务的完整收尾（改了什么、部署了什么、怎么验证、怎么回滚、留下什么坑），
**默认不代表现状** —— 现状以 `../PROJECT_HANDOFF.md` 顶部与 `../docs/topics/` 为准。

先读台账：[`WRAPUP_LEDGER.md`](WRAPUP_LEDGER.md) —— 把下面这些按主题归类，每条给一句话结论与状态
（`现行` / `现行（部分替代）` / `历史` / `待办`），并单列 **11 项未完成**。

## 清单（按时间倒序）

| 收尾文档 | 一句话 | 状态 |
|---|---|---|
| [`WRAPUP_LEDGER.md`](WRAPUP_LEDGER.md) | **收尾台账总表**：23 篇按主题归类 + 状态 + 未完成清单 | 导航 |
| [`WRAPUP_RECORDINGS_OPENING_SAMPLE_20261113.md`](WRAPUP_RECORDINGS_OPENING_SAMPLE_20261113.md) | 录像筛选 + 起手条件 + 详情下载：起手只认 `initial_count`，缺证据单列 | 现行 |
| [`WRAPUP_PIE_INTERSECTIONS_20261112.md`](WRAPUP_PIE_INTERSECTIONS_20261112.md) | 饼图交集阶梯：均摊后排序才画两两交集，网页与 PNG 结果一致 | 现行 |
| [`WRAPUP_PIE_REP_IMAGE_20261115.md`](WRAPUP_PIE_REP_IMAGE_20261115.md) | 前百饼图代表卡无图纯蓝：代表卡优先有图编号、嵌套卡图兜底、缓存键带图源 | 现行 |
| [`WRAPUP_QQ_DELEGATION_TEMP_PRIVATE_20261112.md`](WRAPUP_QQ_DELEGATION_TEMP_PRIVATE_20261112.md) | QQ 管理员代开通 + 保密群临时私聊白名单修复 | 现行 |
| [`WRAPUP_QUERY_API_ANDROID_20261112.md`](WRAPUP_QUERY_API_ANDROID_20261112.md) | 安卓查询 API 补齐 7 端点，19:33:39 上线（`076ada2`） | 现行 |
| [`WRAPUP_WATCH_TOP200_PRIORITY_20261112.md`](WRAPUP_WATCH_TOP200_PRIORITY_20261112.md) | 前 200 内战最高观战优先级（旧规则按时切批会压后） | 现行 |
| [`WRAPUP_CARD_PROFILE_20261111.md`](WRAPUP_CARD_PROFILE_20261111.md) | 卡片模糊搜索 + 「卡片 关键词」合并成一条聊天记录 | 现行 |
| [`WRAPUP_CARD_USAGE_GAME1_20261111.md`](WRAPUP_CARD_USAGE_GAME1_20261111.md) | 使用量/单卡流向口径：取「最近一场有确证首局」，备牌只报换入证据 | 现行（重要） |
| [`WRAPUP_CARD_WINRATE_ORDER_20261111.md`](WRAPUP_CARD_WINRATE_ORDER_20261111.md) | QQ 单卡胜率按先/后攻拆分 | 现行 |
| [`WRAPUP_REPLAY_429_20261110.md`](WRAPUP_REPLAY_429_20261110.md) | 录像批量下载 429：nginx 独立 20r/s 限流 + 客户端节流重试 | 现行 |
| [`WRAPUP_MATRIX_PRECOMPUTE_20261109.md`](WRAPUP_MATRIX_PRECOMPUTE_20261109.md) | 卡组胜率矩阵 14–17 s → 25–45 ms（关掉页面不用的 series 标注 + 默认视图预计算） | 现行 |
| [`WRAPUP_WEB_MEMORY_SIDE_INFERENCE_20261109.md`](WRAPUP_WEB_MEMORY_SIDE_INFERENCE_20261109.md) | web RSS 60→480 MB 根因：进程内整份加载 171 MB side 推断缓存 | 现行 |
| [`WRAPUP_QQ_BIND_SIDE_PLAYER_20261109.md`](WRAPUP_QQ_BIND_SIDE_PLAYER_20261109.md) | 绑定 MC / 订阅空格 / 换备 / 玩家资料；§8 换备座位口径、§9 只看对手前 200 | 现行 |
| [`WRAPUP_CARD_REPORT_PRECOMPUTE_20261108.md`](WRAPUP_CARD_REPORT_PRECOMPUTE_20261108.md) | 卡片统计与构筑详情预计算（8.7 ms / 5.7 ms / 20.2 ms） | 现行 |
| [`WRAPUP_MEM2G_20261108.md`](WRAPUP_MEM2G_20261108.md) | 2 GB 内存治理 + 安卓查询性能（不能升配的前提下） | 现行 |
| [`WRAPUP_PLAYER_HISTORY_20261108.md`](WRAPUP_PLAYER_HISTORY_20261108.md) | QQ「当月历史」：北京时间当月场次/胜率/排名 + 积分走势图 | 现行 |
| [`WRAPUP_WEB_LATENCY_20261107.md`](WRAPUP_WEB_LATENCY_20261107.md) | 网页耗时优化全量记录：措施→做法→实测→状态，**失败的尝试也如实列出** | 现行 |
| [`WRAPUP_PIE_LEGEND_20261107.md`](WRAPUP_PIE_LEGEND_20261107.md) | 前百饼图与图例以网页为准逐项对齐，改由网站生成保存 | 现行 |
| [`WRAPUP_REPLAY_DECK_NAMES_20261107.md`](WRAPUP_REPLAY_DECK_NAMES_20261107.md) | 回放库卡组名一致性：第 1 局口径 + 索引实时刷新 | 现行 |
| [`WRAPUP_DAILY_CARD_PROBABILITY_20261107.md`](WRAPUP_DAILY_CARD_PROBABILITY_20261107.md) | 每日一卡（按 QQ 号一天固定）+ 概率运算（超几何分布） | 现行 |
| [`WRAPUP_20261106.md`](WRAPUP_20261106.md) | 录像库、接口文档合并、构筑 Side 的当轮收尾 | 历史 |
| [`WRAPUP_REPLAY_CLIENT_20261106.md`](WRAPUP_REPLAY_CLIENT_20261106.md) | 安卓带宽补丁 + 查询延迟诊断 + 录像 503 修复；**含「整文件覆盖」事故** | 历史（教训现行） |
| [`WEB_PERFORMANCE_NIGHT_20261106.md`](WEB_PERFORMANCE_NIGHT_20261106.md) | 2026-11-06 夜间性能专项发布记录（代码 `01bbb6c`） | 历史 |

## 另见（名字带 WRAPUP，但内容是现行机制）

- [`../WRAPUP_CARD_CODE_IDENTITY_20261109.md`](../topics/WRAPUP_CARD_CODE_IDENTITY_20261109.md) —— 卡号身份合并，已归入 [`../topics/README.md`](../topics/README.md)。

## 写收尾的规矩

1. 收尾必须能被**下一个对话**找到：写进本表 + `WRAPUP_LEDGER` + `docs/INDEX.md`；
2. 只写「已完成」等于没写 —— 必须带**版本 sha 前 16、验证方式、回滚路径、遗留限制**；
3. 结论要能被反驳：写清「哪些没验证、哪些不能宣称」。

- [WRAPUP_SIDE_CARD_RESOURCES_20261114.md](WRAPUP_SIDE_CARD_RESOURCES_20261114.md)：连接显式关闭与跨用户锁隔离，已部署，QQ实聊待使用验收。

- [WRAPUP_CARD_WINRATE_POPULAR_20261114.md](WRAPUP_CARD_WINRATE_POPULAR_20261114.md)：单卡胜率新增饼图人数Top5，已部署，真实QQ回复待用户验收。

- [WRAPUP_PIE_REP_IMAGE_20261115.md](WRAPUP_PIE_REP_IMAGE_20261115.md)：前百饼图代表卡无图纯蓝，三层修复并部署，QQ下次取图即生效。

- [WRAPUP_SERIES_AUTO_REFRESH_20261115.md](WRAPUP_SERIES_AUTO_REFRESH_20261115.md)：组合改名不生效的根因（重算被 5000 闸门永久跳过）与按批自愈改法，已部署。
# 最新收尾补充

- [单卡使用量漏人修复](WRAPUP_CARD_USAGE_SESSION_20261115.md)：2026-11-15部署并上传；同批96→100，选场/分母修复及回滚。
- [WRAPUP_RENAME_TIERED_20261115.md](WRAPUP_RENAME_TIERED_20261115.md)：改名只立刻改前端口径（820→31 副），其余 789 副进持久队列由后台空闲时追；已部署。
