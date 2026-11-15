# QQ「前百饼图」代表卡无图纯蓝修复收尾（2026-11-15）

> 结论：无垢大艺术 23% 的纯蓝不是统计错误，是代表卡 `23133136` 没有本地图导致回退扇区色；已修「代表卡选择 + 出图兜底 + 缓存键」三层，已部署并推送。

## 0. TL;DR

| 项 | 结果 |
|---|---|
| 现象 | QQ「前百饼图」无垢大艺术 23% 整片纯蓝，外环只有 12 人（7+5）小类卡图 |
| 根因 | 16:20 `c7d4865` 等价类展开把 canonical `23133136` 加入无垢组；`first_cards` 取最小编号取到它，该号无印刷图 |
| 修复 | `first_cards` 优先本地有图编号；PNG 出图用同片最大嵌套卡图兜底；缓存键纳入卡图可用性签名 |
| 版本 | master `88a020e`，已 push，远端一致 |
| 部署 | 仅 `ygo-web` 重启（2526627→2536080），8787/8788 健康 200；QQ/NapCat 未重启 |
| 结果 | 无垢 `image=36270527`；`latest-pie.png` 1,740,992B / sha `b00e7ff7`；蓝色采样 33,948→12 |
| 回滚 | 备份 `data/config-backups/pie-art-fallback-20261115-1640`；恢复三文件 + 授权重启 web + 重建 roster/饼图 |

## 1. 现象与定位

- 用户贴出的 1600×1600 PNG 与线上 `data/cache/deck-pie/latest-pie.png` **sha256 完全一致**（`7877764f…`），生成于 16:22；总人数 100、无垢 23% 均正确。
- 扇区不是卡图，而是调色板第一色 `#0072B2`；外环的动漫卡图是嵌套小类（7+5=12 人，卡图 `5559570`），只画在 `0.56–0.94R`。
- 线上 `latest.json` 的 items 与 `deck_single` 均正常，排除统计口径问题。

## 2. 根因链

1. 16:20 上线的 `c7d4865`（同名异画按卡号等价类统一）在 `series_naming.build_registry` 里把每个字段的 members 按等价类展开。
2. `data/card-equivalence.json` 有等价类 `23133136 ↔ 101307047`（无垢协奏 耀圣诗团），`23133136` 是 canonical；展开后无垢组成员多出 `23133136`。
3. `deck_pie.first_cards` 只取组内最小编号，`min(codes)` 取到 `23133136`。
4. 该号在 `/opt/ygobot/ygopro/pics`、`data/card-images`、`data/cache/card-art` 均无文件；momobako 与 super-pre 两个 CDN 也都是 404。
5. `top100_pie._render_single_payload` 对没有卡图的切片直接 `<path fill=扇区色>`；无垢是最大切片，拿 `PALETTE[0]`，整片纯蓝。
6. `statistics_pie_image` 按 payload 键缓存 PNG，纯色版本被写进 `latest-pie.png`；数据不变就一直发（`LESSONS.md` L21）。

## 3. 改动（三层）

| 文件 | 改动 | 作用 |
|---|---|---|
| `bot/deck_pie.py` | `first_cards` 增 `_has_image`：先取组内最小编号，没有本地图则取“有图的最小编号” | 从源头保证代表卡可渲染；全部命名组生效，不改命名/统计口径 |
| `bot/top100_pie.py` | `_render_single_payload`：代表卡仍无图时，用同片 `players` 最大的 member band 卡图兜底；仍无图才铺色 | 即使某组没有任何代表卡图，也不再整片纯色 |
| `bot/statistics_pie_image.py` | `_key` 增 `_art_signature`：记录每个切片/嵌套卡图在本地是否存在 | 卡图补上后自动生成新图，失败结果不再长期缓存（L21） |
| `tests/test_pie_rep_image.py` | 新增 3 项：无图 canonical 回退、配置代表卡优先、全无图保持最小编号 | 防回归 |

- `first_cards` 的图源检查覆盖 `data/card-images`、`<root>/ygopro/pics`、`data/cache/card-art`，与 `top100_pie._card_file` 的读取路径一致。
- 只改代表卡与出图兜底：不改编组、阈值、候选、选择、拼接、统计口径，也不改卡图取源/CDN 规则。

## 4. 验证

- `first_cards('/opt/ygobot/data')`：无垢=`36270527`；16:45 定时 roster 重建后仍为 `36270527`。
- `/statistics/api/deck-pie.png`：200；16:41 为 807,628B/2.48s，16:45 数据刷新后 811,223B/2.22s。
- `latest-pie.png` 最终 1,740,992B，sha `b00e7ff7…`；蓝色采样像素 33,948→12（仅剩标签圆点）。
- QQ 路径 `render_parts`（独立进程读盘）：pie 1,740,992B / `b00e7ff7`，legend 372,976B，covered 100。
- 服务：`ygo-web` 2536080 active，8787/8788 健康 200；`ygo-qq`/`ygo-collector` active；web 无 error 日志。
- 测试：新增 3/3 通过；原 `test_deck_pie` / `test_pie_card_art` / `test_pie_intersections` 共 11 项，10 通过、1 个 `test_deck_pie` 既有中文编码错误（改前同现，与本次无关）。
- 代表卡变化：按“有图最小编号”规则，实际只影响无垢（`23133136→36270527`）和白鸦（→`63928793`），都是“没图→有图”。

## 5. 部署与版本

| 文件 | 部署 raw sha256 前 16 | 备份 raw sha256 前 16 |
|---|---|---|
| `bot/deck_pie.py` | `216e11d06b600cb7` | `6463a5872976b041` |
| `bot/top100_pie.py` | `5c27f68cbe2899cd` | `d10dbe7d87adbc43` |
| `bot/statistics_pie_image.py` | `aae0f207fa5bc661` | `eedd854dd6ad9253` |

- 备份：`data/config-backups/pie-art-fallback-20261115-1640/`（含三文件与 `SHA256SUMS`）。
- 部署前已核对生产三文件与 git HEAD 逐字节一致（LF 归一）。
- 重启：仅 `ygo-web`；`ygo-qq`/`ygo-collector`/NapCat 未动（饼图由 web 生成落盘，QQ 只读 `latest-pie.png`/`latest-legend.png`）。
- 重建：手动 `systemctl start ygobot-roster-precompute.service` 后无垢 image 变为 `36270527`；16:45 定时任务再次重建仍正确。
- GitHub：master `88a020e`，`ls-remote` 与本地一致。

## 6. 影响与限制

- 影响面：统计网页的单环饼图数据（`deck_single.slices[].image`）与 QQ/网页 PNG；图例数据不变。
- 16:45 榜单自然刷新后「其他 28% / 三幻魔 8%」是数据更新，不是本次改动。
- 未重启 QQ：QQ 进程内仍是旧 `first_cards`，但主路径只读网站落盘的 `latest-pie.png`；若网站不可用触发 QQ 自算兜底，旧进程仍可能走旧逻辑，属于低概率降级，不在本轮扩大重启范围。
- 网页交互式 SVG 对“整个组连一个成员图都没有”的极端情况仍可能显示灰白；本轮只保证 PNG/QQ 有嵌套卡图兜底。
- `_FIRST_CACHE` 仍按配置/参考表时间戳缓存；如果某组当前无任何图、之后才补图，代表卡选择要等下次进程启动/配置变化才重选，但 PNG 缓存键会先失效并重画。

## 7. 回滚

```bash
# 1) 恢复三文件（先核对备份与当前差异）
cp -a /opt/ygobot/data/config-backups/pie-art-fallback-20261115-1640/deck_pie.py \
      /opt/ygobot/data/config-backups/pie-art-fallback-20261115-1640/top100_pie.py \
      /opt/ygobot/data/config-backups/pie-art-fallback-20261115-1640/statistics_pie_image.py \
      /opt/ygobot/bot/
chown ygo:ygo /opt/ygobot/bot/{deck_pie,top100_pie,statistics_pie_image}.py
# 2) 经维护者授权重启 web
systemctl restart ygo-web
# 3) 重建 roster 与饼图
systemctl start ygobot-roster-precompute.service
# 然后请求 /statistics/api/deck-pie.png（或等 QQ 下次取图）
```

不恢复数据库；不重启 QQ/NapCat；回滚后代表卡会回到 `23133136`（纯蓝），这是旧行为。

## 8. 证据与文件

- 证据目录：`workflow-results/qq-pie-check-20261115/`
  - `received.png`：用户贴图（sha `7877764f…`，与线上旧 `latest-pie.png` 一致）
  - `compare.png`：现状 vs 修复预览
  - `final-latest-pie.png`：最终线上 `latest-pie.png`（sha `b00e7ff7…`）
  - `fallback-pie-1600.png`：代表卡仍无图时的嵌套卡图兜底验证
  - `live-deck.json`、`roster-cache.json`：当时的 `deck_single` 与 roster 证据
- 相关文档：`LESSONS.md` L21（缓存会让失败长期存活）、`WRAPUP_PIE_LEGEND_20261107.md`、`WRAPUP_PIE_INTERSECTIONS_20261112.md`
- 交接：`PROJECT_HANDOFF.md` 顶部 2026-11-15 16:45 条
