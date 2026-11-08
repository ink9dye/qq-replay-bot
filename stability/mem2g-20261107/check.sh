#!/usr/bin/env bash
# 只读核对：基线 hash、roster.json 年龄、timer 状态、内存/swap 现状。
set -uo pipefail

ROOT=${1:-/opt/ygobot}
BASE_SHA=a4e2552b7e1c08c02fe536813d282fbb456e703e6771a369c49c138e8d9decff
PATCHED_SHA=3073f8d61285e7880fa46c4237cd491e8e5d4acb78b9f423d4bd4af324ac8a82

echo "=== portal hash ==="
have=$(sha256sum "$ROOT/bot/statistics_portal.py" | cut -d' ' -f1)
echo "live: $have"
case "$have" in
    "$BASE_SHA") echo "state: BASE (patch not applied)" ;;
    "$PATCHED_SHA") echo "state: PATCHED (mem2g applied)" ;;
    *) echo "state: OTHER (another change landed; do not apply)" ;;
esac

echo "=== roster.json ==="
if [ -f "$ROOT/data/cache/roster.json" ]; then
    stat -c 'mtime=%y size=%s' "$ROOT/data/cache/roster.json"
    python3 - "$ROOT/data/cache/roster.json" <<'PY'
import json, sys, time
item = json.load(open(sys.argv[1], encoding='utf8'))
print('saved=%.0f age=%.0fs bytes=%s elapsed=%ss' % (
    item['saved'], time.time() - item['saved'], item.get('bytes'), item.get('elapsed')))
PY
else
    echo "missing"
fi

echo "=== timer ==="
systemctl is-enabled ygobot-roster-precompute.timer 2>/dev/null || true
systemctl is-active ygobot-roster-precompute.timer 2>/dev/null || true
systemctl list-timers ygobot-roster-precompute.timer --no-pager 2>/dev/null | sed -n '1,3p'

echo "=== cgroup ==="
for unit in ygo-web ygo-collector; do
    d=/sys/fs/cgroup/system.slice/$unit.service
    [ -d "$d" ] && printf '%-15s current=%sMB swap=%sMB high_events=%s\n' "$unit" \
        "$(( $(cat $d/memory.current) / 1048576 ))" \
        "$(( $(cat $d/memory.swap.current) / 1048576 ))" \
        "$(awk '/^high /{print $2}' $d/memory.events)"
done
echo "=== memory pressure ==="
cat /proc/pressure/memory | sed -n '1,2p'
free -m | sed -n '1,3p'
swapon --show
