# 前百饼图与图例收尾（2026-11-07）

> 范围：`bot/top100_pie.py`、`bot/statistics_pie_image.py`、`bot/statistics_portal.html`（只这三处）。
> 结论：图片与网页图例**逐项一致**；网站负责生成并落盘“饼图 + 图例”，机器人只读文件；统计口径（`deck_pie.py`）未改。

## 0. TL;DR

| 项 | 结果 |
|---|---|
| 切片顺序 | 与网页同款：人数降序、同人数按名称、「其他」最后 |
| 交集 | 相邻大类画阶梯边界；`primary + 单个 extras` 才算相邻交集；不相邻的交集只在图例列一行 |
| 图例 | 每块**行之和 = 表头人数**；跨字段标 `∩主字段`；独立卡组至少列自身；「其他」表头=切片人数 |
| 版式 | 标签 14px/600/#173529 贴环；卡图压暗 0.22；三栏按内容平衡；中心数字与副标题不再重叠；脚注紧跟内容 |
| 产物 | `data/cache/deck-pie/latest.png`（整图）、`latest-pie.png`（1600²）、`latest-legend.png`（1600 宽）、`latest.json` |
| 机器人 | 先读上面两张（0.08s），过期才请求网站重算（2.27s），网站不可用才自算 |
| 线上 | `top100_pie b1a47c2e`、`statistics_pie_image 32be456b`（RENDER_VERSION 21）、`statistics_portal.html e707b06c` |
| GitHub | `196146f`（v20）、`c081e76`（v21），分支 `delivery/query-perf-20261106` |

## 1. 背景

QQ「前百饼图」原先由 QQ 进程自己画，与网页 `renderSinglePie` 的观感有差异：切片顺序、交集阶梯、图例人头/分栏、标签样式都对不上；另外缓存图会落后于网页数据。目标：**以网页为准逐项对齐**，并把“生成”收敛到网站一侧。

## 2. 最终口径（网页与图片共用同一套规则）

### 2.1 切片

- 排序：`kind!='other'` 按 `players` 降序、同人数按名称；「其他」固定最后（网页 `items.sort`）。
- 百分比：用后端 `deck_single.slices[].percent`；保留两位小数、去掉无意义的 `.0`。
- 颜色：用后端 `color`（无垢 #0072B2、混沌 #E69F00、时黑 #009E73、三幻魔 #D55E00、盈彩 #CC79A7、其他 #C9D2DA）。

### 2.2 交集（阶梯）

- 只对**排序后相邻**的两片画阶梯：外段 `a1+w/2`、内段 `a1-w/2`，在 `b0=0.56R` 处横拐；`w = 交集人数 × 360/total`。
- 相邻交集判定：先看 `bands` 里 `partner` 对方，再用 `deck_pie.middle` 补——只认 `extras` 恰好 1 个、且两边都不是「其他」。
- 不相邻的交集**不画阶梯**（单环没有可共用的边界），只在图例里列 `· 交集 A∩B N人（内环/外环各一张卡图）`。

### 2.3 图例

- 每块表头：`名称 {参与该字段的玩家数}人 · {percent}%`；「其他」用切片人数（= 下面 parts 之和）。
- 行规则：列出**所有带该字段的卡组**（`deck_pie.outer` 中 `primary==字段` 或 `extras` 含字段）；跨字段的行尾标 `∩主字段`；`◆` 表示该卡组占比 >4%。
- 独立卡组（`kind=deck`，如三幻魔）至少列出它自己，避免“只有纯卡组”时整块空白。
- 三栏：按内容高度取“最高一栏最矮”的切法，和网页 CSS `columns` 的观感一致。

### 2.4 版式

- 标签：圆点 `r=3.2` 贴 `r1+16`（±4,-5），文字在点外 10px，`14px/600/#173529`。
- 卡图压暗：`#0a1812` 透明度 0.22（母片与小类带相同）。
- 中心：`100` 42px、副标题 15px，上下各 24px，不重叠。
- 脚注：紧跟图例最后一行，不再固定画布底部（避免机器人图例图大片留白）。

## 3. 架构：网站生成保存，机器人取图

```
网页 /statistics ──(render)──► data/cache/deck-pie/
                                  latest.png          整图（key + PNG）
                                  latest-pie.png      饼图 1600×1600
                                  latest-legend.png   图例 1600 宽
                                  latest.json         人数/类别元数据
QQ「前百饼图」 ──(读文件)──► latest-pie.png + latest-legend.png   （0.08s）
                └─(文件超过 10 分钟)─► curl 127.0.0.1:8788/statistics/api/deck-pie.png（2.27s，由网站重算并落盘）
                └─(网站不可用)─► 本进程兜底自算（旧行为）
```

- 缓存 key = `sha256(RENDER_VERSION + deck_single)` 前 24 位，写在 PNG 前 24 字节 + 换行；改渲染代码只要 bump `RENDER_VERSION` 就会自动失效。
- `/statistics/api/deck-pie.png`：`_roster_reply()`（读预计算/缓存名册）→ `render_from_roster()` → 落盘四件套 → 返回整图。
- 机器人侧 `render_parts()`：`_load_parts()` 命中即返回；否则 `_refresh_from_site()`（读 `data/statistics-owner.key` 换会话 Cookie 再取图）；再不行才自算。

## 4. 部署记录（2026-11-07）

| 版本 | top100_pie | statistics_pie_image | statistics_portal.html | 说明 |
|---|---|---|---|---|
| v17 14:12 | `a9daa3e0` | `bf65ab41`（v17） | 未改（仍 `268237f8`） | 排序/阶梯/人头/标签/分栏与网页对齐；网站落盘饼图+图例 |
| v18 14:16 | `575c5586` | `7a32eb6b`（v18） | `1452d88c` | 独立卡组块列出关联卡组（三幻魔→混沌三幻魔） |
| v20 14:21 | `4e28d762` | `80184ce2`（v20） | `3010ba01` | 表头人数 = 各行之和；跨字段标 ∩主字段；其他=切片人数 |
| v21 21:06 | `b1a47c2e` | `32be456b`（v21） | `e707b06c` | 独立卡组至少列自身；不相邻交集进图例 |

- 每次部署前备份：`/root/pie-align-20261107-backup/`（v17 前的原始文件）、`-backup-v18/`（v17）、`-backup-v19/`（v18）、`-backup-v21/`（v20）。
- `ygo-web` 每次改 py 后重启；`ygo-qq` 于 14:24:32 由用户要求重启（MainPID 791429），14:30:31 另一会话因 service.py 修复再次重启（793810）；**v21 未重启 QQ**（图片由网站生成落盘，QQ 只读文件）。
- `statistics_portal.py` 未动（mem2g 的 3 处性能钩子保持 `3073f8d6…`）。

## 5. 验证（21:00 快照，网页 DOM vs 图片）

| 块 | 表头 | 行数 | 行和 | 网页 | 图片 |
|---|---|---|---|---|---|
| 无垢大艺术 | 26人 · 25% | 7 | 26 | ✓ | ✓ |
| 混沌 | 20人 · 17.5% | 20 + 2 交集 | 20 | ✓ | ✓ |
| 三幻魔 | 9人 · 9% | 1 | 9 | ✓ | ✓ |
| 时黑 | 9人 · 7.5% | 7 | 9 | ✓ | ✓ |
| 其他 | 41人 · 41% | 21 | 41 | ✓ | ✓ |

- `/statistics/api/deck-pie.png` 200（约 800 KB），与 `latest.png`（去掉 24 字节 key）逐字节一致；
- 机器人侧 `render_parts`：命中 0.08s、强制过期后刷新 2.27s；
- 网页与图片的切片顺序、标签、阶梯、分栏一致（见 `workflow-results/pie-compare-20261107/`）。

## 6. 数据侧注意

1. **缓存图“看起来是旧的”**：`latest.png` 只在网站被请求时重画；网页不自动刷新它。页面数据更新后，先打一次 `/statistics/api/deck-pie.png`（或等机器人取图）即可。
2. **三幻魔/混沌没有交集**：交集要求字段进入 `extras`，而字段门槛是“≥3 个不同卡组名”。当前只有 `三幻魔`、`混沌三幻魔` 两个名字 → 不进 `extras`，`混沌三幻魔` 整份计混沌。这是数据口径，不是渲染问题。
3. **single_ring 的排序用均摊前人数、标签用均摊后百分比**：所以个别切片顺序看起来“不是严格降序”，与网页一致（网页也按均摊后数值排）。若后续要改，需网页与图片一起动。

## 7. 明确未做（2026-11-07 21:10 用户决定）

- 不把 `三幻魔` 强行塞进 `qualified`、不改 0.5/0.33 均摊规则、不按卡组名兜底造交集；
- 候选模块 `bot/deck_pie_candidate.py` 已从线上删除，`deck_pie.py` 未改（线上 `bf484097…`）。

## 8. 回滚

```bash
# 回到 v20
cp -a /root/pie-align-backup-v21/* /opt/ygobot/bot/
chown ygo:ygo /opt/ygobot/bot/{top100_pie,statistics_pie_image}.py /opt/ygobot/bot/statistics_portal.html
systemctl restart ygo-web
# 回到 v18
cp -a /root/pie-align-20261107-backup-v19/* /opt/ygobot/bot/
# 回到 v17
cp -a /root/pie-align-20261107-backup-v18/* /opt/ygobot/bot/
```

`statistics_portal.html` 逐请求读盘，替换后免重启；两个 `.py` 需要重启 `ygo-web`。

## 9. 文件清单

- 代码：`bot/top100_pie.py`、`bot/statistics_pie_image.py`、`bot/statistics_portal.html`
- 缓存：`/opt/ygobot/data/cache/deck-pie/{latest.png,latest-pie.png,latest-legend.png,latest.json}`
- 证据：`workflow-results/pie-compare-20261107/`（网页截图、接口图、并排对比、最终三张图）
- GitHub：`delivery/query-perf-20261106` 的 `196146f`、`c081e76`
