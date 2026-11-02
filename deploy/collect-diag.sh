#!/bin/bash
# Collect a redacted diagnostic bundle for troubleshooting.
set -u
STAMP=$(date +%Y%m%d-%H%M%S)
OUT=/tmp/ygobot-diag-$STAMP
mkdir -p "$OUT"

{
  date
  uptime
  free -h
  df -h /
  systemctl is-active ygo-collector ygo-web ygo-qq 2>/dev/null
} > "$OUT/system.txt" 2>&1

journalctl -u ygo-collector -n 500 --no-pager > "$OUT/collector.log" 2>&1
journalctl -u ygo-web -n 200 --no-pager > "$OUT/web.log" 2>&1
journalctl -u ygo-qq -n 300 --no-pager > "$OUT/qq.log" 2>&1

if [ -d /opt/ygobot/data/logs ]; then
  tar czf "$OUT/logs.tgz" -C /opt/ygobot/data logs
fi

sqlite3 /opt/ygobot/data/bot.sqlite3 "pragma integrity_check;" > "$OUT/db-integrity.txt" 2>&1

python3 - <<'PY' > "$OUT/config-summary.txt" 2>&1
import json

SECRET = ('password', 'token', 'secret', 'cookie', 'key')
for name in ('config.json', 'data/watch-config.json'):
    path = '/opt/ygobot/' + name
    try:
        data = json.load(open(path, encoding='utf-8'))
    except Exception as exc:
        print(name, 'ERROR', exc)
        continue
    print('==', name)

    def walk(node, prefix=''):
        for key, value in node.items():
            if isinstance(value, dict):
                walk(value, prefix + key + '.')
            elif any(word in key.lower() for word in SECRET):
                print(prefix + key, '= <redacted>')
            elif isinstance(value, (str, int, float, bool)) or value is None:
                print(prefix + key, '=', value)

    walk(data)
PY

tar czf "$OUT.tgz" -C /tmp "ygobot-diag-$STAMP"
echo "$OUT.tgz"
