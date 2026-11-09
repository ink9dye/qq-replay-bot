# 卡组胜率（matrix）提速：关标注 + 默认视图预计算（2026-11-09）

## 1. 结论

- 页面默认（200/200、first=all）从 **14–17 s** 降到 **25–45 ms**（预计算命中）；
- 非默认（100/100、first=first）从 14–16 s 降到 **3.6–4.1 s**（关闭无用的 series 标注）；
- 200/200 的计算从 **59 s** 降到 **8.1 s**，输出逐字节一致。

## 2. 根因

`statistics_matrix.query` 调 `eligible_views` 时默认 `annotate_series=True`，对全部视角逐构筑算系列标签；matrix 的页面与代码完全不使用 `series` 字段：

| 场景 | 改前 | 改后 |
|---|---:|---:|
| 100/all | 15.3 s | 3.52 s（对拍 `equal=True`） |
| 200/200 | 59 s（annotate 44.7 s） | 8.1 s |

另外，页面默认 200/200 是**最重**的组合，且 matrix 结果缓存只有 30 s，冷启动每次都要等 14–17 s（先 503、前端重试）。

## 3. 改动

| 文件 | 内容 |
|---|---|
| `bot/statistics_matrix.py` | `eligible_views(..., annotate_series=False)` |
| `bot/statistics_portal.py` | 新增 `matrix_precomputed_scope()` / `_precomputed_matrix()`；默认参数命中 `data/cache/matrix-200-200.json`（TTL 1800 s），其余请求走原路径 |
| `tools/precompute_card_report.py` | 每轮追加 matrix 200/200（`--matrix-interval 1800`），写 `data/cache/matrix-200-200.json`（~408 KB） |

定时器沿用 `ygobot-card-report-precompute.timer`（15 分钟）；matrix 每 30 分钟刷新一次。

## 4. 部署与验收（2026-11-09 16:20）

- 版本：`statistics_matrix.py 3874c6de`、`statistics_portal.py 92d2c6e6`、`precompute_card_report.py a7f19bec`；备份 `data/config-backups/matrix-precompute-20261109/`。
- 首轮：card 100/100 20.7 s、200/200 33.9 s、matrix 8.2 s / 408,836 B。
- `ygo-web` 16:22 重启（无录制），health=200。
- 验收：默认 matrix **44.4 / 24.9 ms** `precomputed`（sha 与文件一致）；100/100 与 first=first 首次 503@1 s，随后 4.1 s / 3.6 s 完成；web RSS 123 MB、`memory.events high=0`。

## 5. 回滚

```bash
cp -a /opt/ygobot/data/config-backups/matrix-precompute-20261109/statistics_matrix.py /opt/ygobot/bot/
cp -a /opt/ygobot/data/config-backups/matrix-precompute-20261109/statistics_portal.py /opt/ygobot/bot/
cp -a /opt/ygobot/data/config-backups/matrix-precompute-20261109/precompute_card_report.py /opt/ygobot/tools/
cd /opt/ygobot && python3 -m py_compile bot/statistics_matrix.py bot/statistics_portal.py tools/precompute_card_report.py
systemctl restart ygo-web
```

## 6. 后续（未做）

- matrix 的 `inherit_first_game_types` 仍占 5–6 s（200/200），可加短 TTL 缓存；
- `stat_files` 存活检查 1.85 s 可批量化；
- matrix 缓存 TTL 30 s 可放宽（目前默认视图已走预计算，影响变小）。
