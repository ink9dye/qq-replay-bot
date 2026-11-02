#!/bin/bash
# Compressed SQLite snapshot, keep 3 days locally.
set -eu
DATA=/opt/ygobot/data
DEST=$DATA/backup
mkdir -p "$DEST"
TMP=$(mktemp /tmp/ygobot-backup-XXXXXX.sqlite3)
sqlite3 "$DATA/bot.sqlite3" ".backup '$TMP'"
gzip -c "$TMP" > "$DEST/bot-$(date +%F).sqlite3.gz"
rm -f "$TMP"
find "$DEST" -name 'bot-*.sqlite3.gz' -mtime +3 -delete
echo "backup ok: $DEST/bot-$(date +%F).sqlite3.gz"
