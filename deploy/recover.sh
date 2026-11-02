#!/bin/bash
# One-command status / restart / diagnostics.
case "${1:-status}" in
  status)
    systemctl is-active ygo-collector ygo-web ygo-qq 2>/dev/null
    docker ps --filter name=napcat --format 'napcat: {{.Status}}' 2>/dev/null
    ss -lntp | grep -E ':8787|:8788|:3001' || echo "no ports"
    free -h | head -2
    df -h / | tail -1
    ;;
  restart)
    systemctl restart ygo-collector ygo-web
    if systemctl cat ygo-qq >/dev/null 2>&1; then systemctl restart ygo-qq; fi
    docker restart napcat 2>/dev/null || true
    sleep 5
    systemctl is-active ygo-collector ygo-web ygo-qq 2>/dev/null
    ;;
  diag)
    /opt/ygobot/tools/collect-diag.sh
    ;;
  logs)
    journalctl -u ygo-collector -n 80 --no-pager
    journalctl -u ygo-web -n 40 --no-pager
    journalctl -u ygo-qq -n 80 --no-pager
    ;;
  *)
    echo "usage: recover.sh status|restart|diag|logs"
    exit 2
    ;;
esac
