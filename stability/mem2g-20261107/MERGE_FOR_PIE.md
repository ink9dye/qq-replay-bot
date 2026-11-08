# 给饼图会话的合并说明（statistics_portal.py）

日期：2026-11-07 21:05。背景：另一个会话在改饼图（`/statistics/api/deck-pie.png`、`statistics_pie_image.py`、`cached <玩家>` 日志），会继续改 `bot/statistics_portal.py`。本文只说明**哪些改动必须保留**，避免整文件覆盖后丢性能钩子。

## 1. 现在线上是什么版本

| 项 | 值 |
|---|---|
| 线上 `bot/statistics_portal.py` | sha256 `3073f8d61285e7880fa46c4237cd491e8e5d4acb78b9f423d4bd4af324ac8a82` |
| 它 = 饼图会话的 `a4e2552b…`（含 `deck-pie.png` 路由）+ mem2g 的 3 处改动 | — |
| mem2g 补丁包 | `stability/mem2g-20261107/`（`03-apply.py`、`03-portal.diff`） |

## 2. 必须保留的 3 处（都很小）

1. **常量区**：`PRECOMPUTED_ROSTER_SECONDS = 900.0` 和 `_trim_heap()`（`ctypes` 调 `malloc_trim`，非 glibc 静默跳过）。
2. **`_roster_reply` 开头**：
   ```python
   if not force:
       precomputed = self._precomputed_roster()
       if precomputed is not None:
           return precomputed
   ```
   配套 `_precomputed_roster()` 方法：读 `data/cache/roster.json`，900 秒内直接返回，带 `X-Statistics-State: precomputed`。
3. **`_cached_query_reply_locked` 里 `result=self._query_reply(callback)` 之后**：
   ```python
   if key and key[0] in ('roster','matrix','card-report','recordings'):
       _trim_heap()
   ```
4. **（2026-11-08 新增）卡牌 samples 截断**：
   - 常量区 `CARD_SAMPLES_LIMIT = 200`；
   - `card-metrics` 的 `run(db)` 里 `result['samples'].sort(...)` 之后：
     ```python
     if args.get('full',['0'])[0]!='1' and len(result['samples'])>CARD_SAMPLES_LIMIT:
         result['samples']=result['samples'][:CARD_SAMPLES_LIMIT]
         result['samples_truncated']=True
     ```
   - 配套 `statistics_card_metrics.js` 的 `new URLSearchParams({code,full:'1',partition:`。
   线上版本：portal `df6f8d50…`、js `46957a3a…`。
5. **（2026-11-08 22:47）investment 只给网页**：
   ```python
   full_request=(args.get('full',['0'])[0]=='1'
                 or 'Python-urllib' not in (headers.get('User-Agent') or ''))
   ...
   if full_request:
       result['investment']=player_investment(...)
   ```
   线上版本：portal `ffa0ce7d…`。
6. **（2026-11-08 23:05）card-metrics 进程内缓存**：
   - `__init__` 加 `self._card_metrics_cache={}` / `self._card_metrics_lock`；
   - 类里加 `_card_metrics_cache_get/put`（TTL 600 s，32 条/12 MB）；
   - `card-metrics` 的 `run` 外面：先查缓存，未命中算完 `put` 再返回，并 `_trim_heap()`。
   线上版本：portal `17a655cd…`。

## 3. 为什么饼图会话要保留它

`deck-pie.png` 里调了 `self._roster_reply()` 再 `render_from_roster(...)`。

- **保留**：`_roster_reply()` 读预计算文件（75 KB，毫秒级），饼图渲染前不再触发 roster 重算；
- **丢掉**：`_roster_reply()` 回到进程内计算，`deck-pie.png` 每次冷启动可能等 9–90 秒，还会占 `_query_compute_lock`，把 player/card 查询一起堵到 503。

预计算文件由 `ygobot-roster-precompute.timer` 每 4 分钟生成一次，和饼图逻辑无关，不需要饼图会话维护。

## 4. 如果整文件覆盖了怎么办

1. 先看 `sha256sum bot/statistics_portal.py`；
2. 若等于 `3073f8d6…`，说明没丢，不用动；
3. 若变成别的值（饼图会话的新版本），把 `03-apply.py` 里的 `BASE_SHA` 改成新 hash，重跑一次 dry-run：
   ```bash
   python3 /root/mem2g-20261107/03-apply.py --root /opt/ygobot --dry-run
   ```
   锚点（`_roster_reply` 定义、`result=self._query_reply(callback)`）通常还在；通过后正式打，再 `systemctl restart ygo-web`。
4. 如果锚点被改得对不上，`03-apply.py` 会明确报 `REFUSE: ... anchor not found`，不会写坏文件；按 §2 手工补 3 处即可。

## 5. 验证（30 秒）

```bash
cd /opt/ygobot
python3 - > /tmp/ck.txt <<'PY'
import hmac, hashlib, time
from bot.statistics_portal import owner_secret
s=owner_secret('/opt/ygobot/data/statistics-owner.key').encode(); e=str(int(time.time())+600)
print(e+'.'+hmac.new(s,e.encode(),hashlib.sha256).hexdigest())
PY
curl -s -D - -o /dev/null -H "Cookie: qqstats_owner=$(cat /tmp/ck.txt)" \
  -H 'Host: 127.0.0.1:8788' http://127.0.0.1:8788/statistics/api/roster | grep -i x-statistics
rm -f /tmp/ck.txt
```

看到 `X-Statistics-State: precomputed` 就是好的；看不到就是钩子丢了。
