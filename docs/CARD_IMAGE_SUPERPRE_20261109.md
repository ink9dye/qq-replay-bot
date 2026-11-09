# 卡图串卡：超先行同号图不再可信（2026-11-09）

## 一、现象

构筑页点开 `#100268010 沉默斗者-沉默魔术师`，弹窗左边却是「永恒混沌」（25750986，罠卡）；
牌桌格子里同一张也是错图。卡名、描述、构筑与统计都正确。

## 二、根因（两层）

1. **卡号 ≥100000000 是先行/超先行号段，换包后会改指另一张卡。**
   - `cdn.233.momobako.com/ygopro/pics/100268010.jpg` 现在是「永恒混沌」（70,446 B，HTTP 200）；
   - 同号的正确图只在 `cdntx.moecube.com/ygopro-super-pre/data/pics/100268010.jpg`
     （340,523 B，官方 SAMPLE），与游戏 `expansions/test-release.cdb` 的
     `100268010 = 沉默斗者-沉默魔术师` 一致；
   - 同批 `100268001 = 业火之魔神 艾克佐迪亚`、`100268003 = 鹰身女郎-华丽的分身-`，
     momobako 上却是两张「卡通」卡。

2. **有三个入口还会先问 momobako，且它返回 200 而不是 404：**
   - 弹窗大图写死 `momobako/<code>.jpg!half`，`onerror` 不触发，永远回退不到正确源；
   - `statistics_local_cards._schedule`（补 `pics/`）先 momobako、后 super-pre；
   - 11-03 批量生成的相缩略图也是从 momobako `!thumb2` 落盘，
     `100268001/003/010` 三张因此是卡通卡 / 永恒混沌。
   （`ensure_full`/`ensure_thumb` 在 11-06 起已按 super-pre 优先，只影响上述入口与老缓存。）

## 三、改动（已上线，2026-11-09 11:2x）

代码（`bot/`）：

| 文件 | 改动 |
|---|---|
| `statistics_build.js` | 大图先走 `/statistics/card-image/<code>`；新增 `cardImageRemote()`：≥1e8 用 super-pre 原图、其余 momobako `!half`；弹窗 sources = [服务端, 远端正确源, 缩略图] |
| `statistics_local_cards.py` | `_schedule` 对 ≥1e8 先 super-pre；`_thumb_file` 映射只兜底且 ≥1e8 不查；`picture()` 命中 `data/card-images/<卡号>.jpg` |
| `card_image_cache.py` | 映射只认可信值（普通卡 → ≥1e8 先行图号）；超先行不写不读；写前校验百鸽页未跳转；`load_map` 过滤坏条目 |

数据（生产）：

- `100268001/003/010` 用 super-pre 原图覆盖 `ygopro/pics/` 与 `data/card-images/`，
  删旧 thumb 后离线重缩：9,088 / 8,172 / 9,206 B，113×165。
- `data/card-image-ids.json` 里没有这三张的条目（坏映射不是本次直接原因），无需清理。
- 备份：`data/config-backups/card-image-mapping-20261109-105830/`（两份 py 原文件）。

## 四、验证

带 owner cookie 直接请求 8788（2026-11-09 11:08）：

| 请求 | 结果 |
|---|---|
| `/statistics/card-image/100268010` | 200 `image/jpeg` **340,523 B**（正确原图） |
| `/statistics/card-thumb/100268010` | 200 `image/webp` **9,206 B**（正确缩略图） |
| `/statistics/card-image/100268003` | 200 `image/jpeg` 317,061 B |
| `/statistics/card-thumb/100268003` | 200 `image/webp` 8,172 B |
| `/statistics/card-image/100268001` | 200 `image/jpeg` 343,188 B |
| `/statistics/card-thumb/100268001` | 200 `image/webp` 9,088 B |
| `/statistics/assets/statistics_build.js` | 200，含 `cardImageRemote`（页面实际引用的是 `assets` 路径，按 LF 归一后 29,368 B） |

- 线上哈希（sha256 前 16）：`statistics_local_cards.py 8a9ab34b…`、`card_image_cache.py f4abfe1e…`、
  `statistics_build.js 67a7071d…`（29,654 B，基于 11:04 的 29,509 B 基线加卡图三行，`node --check` 通过）；
  `statistics_portal.py 3347a499…` 未改。
- 单测：`tests/test_card_thumbs.py` + `tests/test_card_image_mapping.py` 共 16 项通过
  （含“超先行先取 super-pre”“坏映射不供图”“详情页回退原图”）。
- 重启后 `ygo-web` RSS 76 MB，服务器可用内存 320 → 576 MB。
- 浏览器需 **Ctrl+F5**：`statistics_build.js` 有 1 小时缓存，旧缩略图 URL 有 1 天缓存。

## 五、回滚

- 代码：恢复 `data/config-backups/card-image-mapping-20261109-105830/` 的
  `card_image_cache.py`、`statistics_local_cards.py`；JS 用
  `.tmp-ops/card-image-fix-20261109/rollback/statistics_build.orig.js`（上线前等价版）；
  `systemctl restart ygo-web`。
- 数据：这三张的 `pics/`、`card-images/`、`card-thumbs/` 可留可删（删了会按新逻辑从 super-pre 重下）。

## 六、影响面与内存

- 只影响图：卡名、构筑、统计不变；未重启 `ygo-qq`（观察期）与 `ygo-collector`（录制），
  它们下次自然重启后同逻辑生效。
- **内存零增量**：没有新增缓存/索引/线程；`load_map` 因过滤反而变小；
  详情大图只是多读一个本地文件（≤2 MB，随请求释放）。

## 七、遗留

- 80 个 ≥1e8 缩略图已核对：除上述三张外，momobako 同号均 404（图来自 super-pre，正确）。
- 以后新出现的 `1002680xx` 一律只认 super-pre；若 `statistics_build.js` 被其它会话整文件覆盖，
  需保留 `cardImageRemote` 与 `cardImage(...,true)` 两处改动。
