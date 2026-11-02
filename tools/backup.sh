#!/bin/bash
# Compressed SQLite snapshot, keep 3 days locally.
set -eu
DATA=/opt/ygobot/data
DEST=$DATA/backup
mkdir -p "$DEST"
TMP=$(mktemp /tmp/ygobot-backup-XXXXXX.sqlite3)
# 低优先级跑，别和录制/QQ 抢 CPU
nice -n 10 sqlite3 "$DATA/bot.sqlite3" ".backup '$TMP'"
gzip -c "$TMP" > "$DEST/bot-$(date +%F).sqlite3.gz"
rm -f "$TMP"
# 备份期间 WAL 会涨：收尾做一次截断检查点，避免下一次写入被大 WAL 拖慢（失败不影响备份）
nice -n 10 sqlite3 "$DATA/bot.sqlite3" "PRAGMA busy_timeout=30000; PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
find "$DEST" -name 'bot-*.sqlite3.gz' -mtime +3 -delete
echo "backup ok: $DEST/bot-$(date +%F).sqlite3.gz"
