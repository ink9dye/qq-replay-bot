# 构筑、Side 与排序规则

2026-11-03 生产已加载提交 `9d62844`（部署记录见 `PROJECT_HANDOFF.md` 与 `exports/deck-fix-20261103/`）。本文件说明当前实现，规则冻结见 `LOGIC_FREEZE.md`；两者冲突时以冻结约定和用户最新明确指示为准。

## 1. 选哪一局

默认构筑 = **最新一场比赛的可靠第 1 局**，主卡和额外都取自该局；Side 也以该局为基准。

选择顺序：

1. `stat_evidence` 中有 `capture.session_id` / `game_no` 时，以显式 capture 为准；同一 digest 出现冲突 capture 时视为不可用。
2. 没有显式 capture 时，只使用 `stat_files -> replay_sessions` 的**唯一**关联；同一 digest 有多个不同 session/game_no 时放弃回退，不按时间猜。
3. `statistics_population.latest_views` 以 `(session_id, 双方玩家)` 分组，避免不同对局因同名 session 被合并；每组取最新一场，场内优先 `game_no=1`。
4. 第 1 局缺失、过期、被排除或卡表不完整时，回退到最新有效观测，并把 `observation_kind` 标为 `post_side` / `initial_status_unknown`；**没有唯一首局就不生成 Side 基准**。

相关代码：`bot/statistics_match_identity.py`、`bot/statistics_population.py`。

## 2. Side 怎么推

Side 只使用“有证据的换入/换出”，不从卡组名或使用记录反推。

### 2.1 同场净变化

对每个 session：

- 以该场 `game1` 的主卡+额外为基准；
- `game2`、`game3` 分别与 `game1` 比较，不和上一局链式比较；
- 同一张卡在多局中取**最大新增数量**，不把两局换入数量相加；
- 用换入卡并集减去当前所选局的主卡+额外，得到该局视角的 Side 候选。

### 2.2 历史场次

历史 session 只有同时满足以下条件才参与：

- 其 `game1` 的主卡+额外与当前基准**完全一致**（按已验证异画身份归一后比较）；
- 双方玩家配对稳定、局次唯一、来源有效。

不采用 Demo 的“改动不超过几张”判据，避免把调整构筑误当成 Side 移动。

### 2.3 超过 15 张才筛选

- 观测并集 `observed_copies <= 15`：全部保留，不补齐到 15。
- `> 15`：按以下顺序选到 15 张：
  1. 当前场明确换入的卡优先；
  2. 再按历史支持分：`score = max(Σ exp(-Δt/30天), 1.5 × 同对手卡组标签加权和)`；
  3. 同分时取观测张数多、卡号小的。
- 未选中的卡写入 `latent_cards`，同时保留 `observed_cards`、`sources`、`per_card` 证据；不删除、不伪造。
- 当前场证据本身超过 15 张，或直接 Side + 推测 Side 超过 15，标记 `conflict`，禁止把推测 Side 并入 YDK。

### 2.4 查看第 2/3 局

选中换边局时，Side 候选以**该局主卡+额外为减数**：

- 换入后已在主卡的卡，不再出现在 Side；
- 从主卡换出的卡留在 Side 候选中；
- 因此祈祈巧第 1 局是主卡 0 张宇宙性飓风 / Side 2 张，第 2 局是主卡 2 张 / Side 0 张。

相关代码：`bot/statistics_build_detail.infer_side`。

## 3. 排序

排序只读本副牌涉及的本地卡表（`data/card-library.json` 指向的 `ygopro/cards.cdb` + `expansions/*.cdb`），不联网补参数。实现：`bot/deck_sort.py`。

### 3.1 主卡 / Side

1. 怪兽 → 魔法 → 陷阱；
2. 怪兽：等级/阶级/Link 数降序 → 攻击力降序 → 防御力降序 → 卡号升序；
3. 魔法：通常 → 速攻 → 永续 → 装备 → 场地 → 仪式；
4. 陷阱：通常 → 永续 → 反击；
5. 同子类型内按卡号升序；
6. 未知/缺参数的卡稳定置后。

Link 怪兽数据库的 DEF 位是标记，不作为防御力；等级取 `level & 0xff`，避开灵摆高位。

### 3.2 额外

1. 融合 → 同调 → 超量 → 连接；
2. 同组内按等级/阶级/Link 数降序 → 攻击力降序 → 防御力降序 → 卡号升序；
3. 未知参数稳定置后。

### 3.3 应用位置

- 构筑页 `cards`、`inferred_side.cards`；
- QQ 构筑图（`deck_image.render`，传入同一份 `card_data`）；
- YDK 导出（主卡、额外按 `cards` 顺序；Side 按 `side_order` 合并直接 Side 与推测 Side）；
- 卡组命名详情页卡表。

## 4. 实体张数校验

`statistics_build_detail.validate_build` 在展示和导出前执行：

- 把已验证的异画/同名印刷按 `card_identity` 映射归并；
- 主卡+额外+Side 同一实体合计 ≤3 张；
- 主卡 ≤60、额外 ≤15、Side ≤15；
- 仅当主卡完整（`main_complete`）时检查主卡是否少于 40 张；
- 直接 Side 与推测 Side 重叠取最大值，不重复相加（`Counterside |= inferred`）。

结果：

- `status`: `no_basic_conflict` / `warning` / `conflict`
- `blocking`: 只有分区超量或同一实体 >3 时为真；
- `issues`: 具体冲突；
- `summary`: 可直接展示的中文说明；
- `banlist_checked=false`、`banlist_status=unverified_missing_version`：**禁限表未校验**（缺对应历史版本），不伪造结论。

冲突处理：

- `blocking=true` 时 `inferred_side.conflict=true`，YDK 不允许并入推测 Side；
- 原始记录（主卡/额外/直接 Side/证据）保留；
- 主卡不足 40 只作 `warning`，不阻止推测 Side 导出；
- 网页显示校验摘要；QQ 构筑消息也带一行校验摘要。

## 5. 输出字段

`/statistics/api/build-detail` 与 QQ 构筑共用 `statistics_build_detail.query`：

| 字段 | 含义 |
|---|---|
| `cards` | 所选局主卡/额外/直接 Side，已排序 |
| `completeness` | 主卡/额外/Side 是否完整记录 |
| `inferred_side.cards` | 当前 Side 候选，已排序 |
| `observed_cards` | 筛选前观测并集 |
| `latent_cards` | 超出 15 张未选中的历史候选 |
| `side_order` | 直接 Side + 推测 Side 合并后的排序 |
| `build_validation` | 实体张数与分区校验摘要 |
| `selected` | 选中的 digest、observation_kind 等 |

`ydk()` 在 `include_inferred=True` 时会在 `!side` 前写入
`# inferred Side candidates are included; not a complete side deck`，并按 `side_order` 输出 Side。

## 6. 边界与已知限制

- 没有唯一可靠首局时，不生成 Side 基准（`reason=first_game_unavailable`）。
- 直接 Side 记录完整时，不再推断（`reason=direct_side_complete`）。
- 历史场次只在首局主卡+额外完全一致时合并；不跨版本混用。
- 观测并集不足 15 张时不补齐，Side 可能不完整。
- 禁限表未校验；同名/特殊规则未覆盖。
- `stat_files`/`replay_sessions` 对同一 digest 存在多个不同 session 的少数记录会放弃回退，避免猜错。
- 排序缺本地卡表参数时置后，不联网补。

## 7. 验证

- 生产复现（旧逻辑）：祈祈巧主卡 2 张宇宙性飓风 + Side 2 张；
- 修复后生产 API：默认第 1 局主卡 40、宇宙性飓风 0、Side 推测 2；第 2 局主卡 2、Side 中 0；
- `test_deck_sort` 19 项通过；
- 映射 321 项测试 23F+10E，33 个失败/错误 ID 全部存在于基线 618 项全量运行，无新增失败；
- 构筑页浏览器截图见 `workflow-results/qiqiao-build-page-20261103.png`。

证据：`workflow-results/qiqiao-production-bug-20261103.txt`、`qiqiao-candidate-fix-20261103.txt`、`deck-fix-production-20261103.txt`、`deck-fix-deployment-20261103.txt`、`exports/deck-fix-20261103/manifest.json`。
