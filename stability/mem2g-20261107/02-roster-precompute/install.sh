#!/usr/bin/env bash
# 安装 roster 预计算脚本和 timer。只新增文件，不改任何现有源码。
set -euo pipefail

ROOT=${1:-/opt/ygobot}
HERE=$(cd "$(dirname "$0")" && pwd)
DST="$ROOT/tools/roster_precompute.py"

if [ -e "$DST" ]; then
    echo "REFUSE: $DST already exists (another change?)" >&2
    exit 3
fi
if [ ! -f "$ROOT/bot/statistics_portal.py" ]; then
    echo "REFUSE: $ROOT/bot/statistics_portal.py not found" >&2
    exit 3
fi

install -o ygo -g ygo -m 755 "$HERE/roster_precompute.py" "$DST"
install -o root -g root -m 644 "$HERE/ygobot-roster-precompute.service" /etc/systemd/system/
install -o root -g root -m 644 "$HERE/ygobot-roster-precompute.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ygobot-roster-precompute.timer
systemctl list-timers ygobot-roster-precompute.timer --no-pager
echo "installed. first run in ~2 min; check: journalctl -u ygobot-roster-precompute.service -n 20"
