# 卡号身份：同名多号合并（2026-11-09）

## 1. 现象

QQ `使用量 决斗者之力`、`单卡流向 决斗者之力` 全是 0：图表有 100 人构筑，但没有一个人"带这张卡"。

## 2. 根因

同一张卡在卡库里有两个号：

| 来源 | 卡号 | 说明 |
|---|---|---|
| 公开卡库（`data/cache/cards.json`） | `71599345` | 正式卡号 |
| 本地 YGOPro（`ygopro/expansions/test-release.cdb`） | `101307067` | 先行服号段（`1013xxxxx`） |

前 100 构筑的 `stat_build_cards.code` 写的是**先行号** `101307067`（全库 10,557 行、27,304 张），
`71599345` 一条记录都没有。

`bot/card_usage_pie.resolve_card` 按卡名匹配时，同名命中多个卡号就取**最小卡号**
（`71599345`），正好落到空号 → 使用量/单卡流向全 0。`单卡胜率`、网页卡片表等
`card_identity` 消费方用的是同一个错号，或干脆把同一张卡拆成两行。

不是个别卡：全库有 **131 个卡名**的数据只落在先行号上、正式号为空；
另有 71 个卡名有 2 个以上号同时有数据（异画）。

## 3. 改动

1. **`bot/card_usage_pie.py`**
   - `resolve_card(data_dir, query, db=None)`：同名多号时，如果原来的最小卡号在
     `stat_build_cards` 里没有记录、而同名另一个号有，就改用有记录的号；其余情况
     保持原口径（不影响本来正常的卡）。
   - 新增 `identity_codes(db, code)`：读 `card_identity` 的异画/先行号组。
   - `collect()` 按组内全部卡号汇总（`code IN (...)`），不再只查一个号。
2. **`bot/card_flow.py`**：同样按身份组汇总。
3. **`bot/card_winrate.py`**：卡名解析改为带 db 解析（数字卡号仍按原样）。
4. **`tools/build_card_equivalence.py`（新增）**：从公开卡库 + 本地 YGOPro 卡库
   生成 `data/card-equivalence.json`；合并条件是**卡名、效果文字（去空白）、
   type/atk/def/level/race/attribute 全一致**，核不上就不合并。
   结果：**478 组 / 1,102 个卡号**，其中 202 组的数据分散在不同号上。

之前 `bot/card_identity.py` 的机制一直在，但 `data/card-equivalence.json`
从来没生成过，所以全站 `identity()` 都退化成"一个卡号"。

## 4. 影响

- 统计口径从「单个印刷号」改为「这张卡」：约 202 张卡的数字变大。
  - 决斗者之力：0 → 67/100 人带；「使用量」主卡 171 张（0张33 / 1张7 / 2张16 / 3张44）。
  - 灰流丽：主卡 120 → 200 张（三个号 30484+14271+6625 合一；前100 全部 2 张）。
- 网页卡片表合并成一行（灰流丽只剩 `14558127`、决斗者之力 `71599345`，
  确认人数 67 与 QQ 单卡流向一致），`identity_version` 变为 `42c519f4…`。
- 单卡胜率、抽牌/对阵、投资、矩阵、Side、录像按卡筛选等所有读 `card_identity`
  的地方一起合并，口径统一。

## 5. 部署与验证

- 线上文件（sha256 前 16）：`card_usage_pie 296582dfa6351df7`、
  `card_flow 73b8c3de275b40c9`、`card_winrate 5b76161321195f90`、
  `tools/build_card_equivalence 95e29ab1612c7279`、
  `data/card-equivalence.json 42c519f488207553`（reference_version `b1e1c0ea35651b96`）。
- 备份：`data/backup/card-code-fix-20261109/`（改动前原版）、
  `data/backup/card-code-identity-20261109/`（第一次修复后、启用身份映射前）。
- 17:05 第一次上线（空号修复）+ 17:11 第二次（身份映射 + 按组合并），两次都重启
  `ygo-qq`；web/collector/replay-api 未重启，8787/8788 health=200。
- 单测 `tests/test_card_code_resolve.py` 5 项通过（空号回落、有记录不动、
  无记录不动、不传 db 保持旧行为、按组汇总）。
- 只读复核：`collect('决斗者之力')` → code 101307067，67/100；`collect('灰流丽')`
  → 100/100、主卡 200 张；网页卡片表 1,005 行、无重复行。
- 代码提交：`736fec9`（`fix/card-code-identity` → 合入 `master`，基线 `23ce102`）。
  本次只提交实际部署的改动，没有把 PC 工作树的其它在改文件带进来。

## 6. 回滚

1. 删映射（立即生效，不需要重启）：`rm /opt/ygobot/data/card-equivalence.json`
   → 全站回到"按单个卡号"统计，空号修复仍然有效。
2. 回滚代码：`cp /opt/ygobot/data/backup/card-code-identity-20261109/* /opt/ygobot/bot/`
   `chown ygo:ygo` → `systemctl restart ygo-qq`。
3. 彻底回到今天之前：用 `data/backup/card-code-fix-20261109/` 覆盖并重启。

## 7. 维护

- 每次本地 YGOPro 卡库更新（新卡包/新先行卡）后重跑：
  `/usr/bin/python3 /opt/ygobot/tools/build_card_equivalence.py && chown ygo:ygo /opt/ygobot/data/card-equivalence.json`
- 不需要重启：`card_identity` 按 mtime 重读，roster 版本随文件变化、缓存自动失效。
- 不重跑也不会再出现"全是 0"（有卡号回落兜底），只是新先行卡暂时不合并。

## 8. 时间线

| 时间 | 事件 |
|---|---|
| 16:50 | 用户报「使用量/单卡流向 决斗者之力 全是 0」 |
| 16:53 | 定位：`resolve_card` 取最小卡号 → 空号 `71599345`；构筑数据在 `101307067` |
| 17:05 | 第一版上线（空号回落），只重启 `ygo-qq`；决斗者之力 → 67/100、171 张 |
| 17:09 | 发现异画号同样分开（灰流丽 3 个号），确认 `card-equivalence.json` 从未生成 |
| 17:11 | 生成映射 + 按组汇总上线（全站启用），再重启 `ygo-qq`；灰流丽 120 → 200 张 |
| 17:20 | 汇总文档、`docs/INDEX.md`、`PROJECT_HANDOFF.md` 更新 |
| 17:30 | 提交 `736fec9` 并推送 `master` |

## 9. 影响面与后续

- **服务**：只重启 `ygo-qq`（两次）；`ygo-web` 未重启，卡片表在进程内缓存自然过期前
  可能仍显示合并前结果（几分钟）。
- **接口/UI**：QQ `使用量`、`单卡流向`、`单卡胜率` 的数字变大（异画合并）；
  网页卡片表/卡片详情/矩阵/Side/投资/录像按卡筛选同口径变化。
- **数据**：新增 `data/card-equivalence.json`（43 KB），不改任何事实表。
- **后续**：
  - 卡库更新后重跑生成工具（见 §7）；若新卡包里出现新的先行号段，先看
    `reference()` 是否加载成功（`data/logs` 无报错、`identity_version` 变化即可）。
  - 没有做真实 QQ 端到端截图：只验证到模块 `render()` 出图（两张图见
    `workflow-results/card-code-fix-20261109/`）与服务层调用参数。
  - 若以后要恢复"按单个印刷号"的旧口径，只删映射文件，代码不用回滚。
