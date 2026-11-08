#!/usr/bin/env bash
# 2 GB 机器的内存腾挪：让 web 的热页别再被 MemoryHigh 压进 swap，并给冷页一块
# 压缩 swap（zram），把 3-4 MB/s 的磁盘换页 I/O 降下来。
#
# 用法：
#   bash 01-memory.sh            # 应用（立即生效，不重启任何服务）
#   bash 01-memory.sh --status   # 只看当前值
#   bash 01-memory.sh --rollback # 回滚到 420M/500M，并移除 zram
#
# 2026-11-07 修正两处：
#   1. OOMScoreAdjust 不接受 systemctl set-property，改成 drop-in + 运行时 /proc 写入；
#   2. MemoryHigh/Max 也用 drop-in（zz-mem2g.conf，排在 memory.conf 之后），
#      否则后面的 daemon-reload 会用旧 drop-in 把 set-property 的值改回去。
set -euo pipefail

WEB_UNIT=ygo-web.service
COLLECTOR_UNIT=ygo-collector.service
ZRAM_SIZE=1G
ZRAM_ALGO=lz4
ZRAM_PRIO=100

status() {
    echo "--- cgroup ---"
    for unit in "$WEB_UNIT" "$COLLECTOR_UNIT"; do
        echo "$unit"
        for key in MemoryHigh MemoryMax OOMScoreAdjust; do
            printf '  %-15s %s\n' "$key" "$(systemctl show -p "$key" --value "$unit")"
        done
    done
    echo "--- swap ---"
    swapon --show
    [ -r /sys/block/zram0/mm_stat ] && echo "zram(mm_stat): $(cat /sys/block/zram0/mm_stat)"
    free -m | sed -n '1,3p'
}

runtime_oom() {
    unit=$1; value=$2
    pid=$(systemctl show -p MainPID --value "$unit")
    if [ -n "$pid" ] && [ "$pid" != "0" ] && [ -w "/proc/$pid/oom_score_adj" ]; then
        echo "$value" > "/proc/$pid/oom_score_adj" || true
    fi
}

apply() {
    # zz- 前缀保证排在 memory.conf（420M/500M）之后，daemon-reload 不会把它改回去。
    printf '[Service]\nMemoryHigh=480M\nMemoryMax=560M\nOOMScoreAdjust=200\n' \
        > "/etc/systemd/system/$WEB_UNIT.d/zz-mem2g.conf"
    printf '[Service]\nOOMScoreAdjust=-500\n' \
        > "/etc/systemd/system/$COLLECTOR_UNIT.d/zz-mem2g.conf"
    systemctl daemon-reload
    # OOMScoreAdjust 只在新进程启动时由 systemd 应用，运行中的进程要直接写 /proc。
    runtime_oom "$WEB_UNIT" 200
    runtime_oom "$COLLECTOR_UNIT" -500

    if swapon --show=NAME --noheadings | grep -qx '/dev/zram0'; then
        echo "zram already active"
    else
        modprobe zram
        if [ ! -b /dev/zram0 ]; then
            zramctl --find --size "$ZRAM_SIZE" --algorithm "$ZRAM_ALGO" >/dev/null
        else
            zramctl --size "$ZRAM_SIZE" --algorithm "$ZRAM_ALGO" /dev/zram0
        fi
        mkswap /dev/zram0 >/dev/null
        swapon -p "$ZRAM_PRIO" /dev/zram0
    fi
    status
}

rollback() {
    rm -f "/etc/systemd/system/$WEB_UNIT.d/zz-mem2g.conf" \
          "/etc/systemd/system/$COLLECTOR_UNIT.d/zz-mem2g.conf"
    systemctl daemon-reload
    runtime_oom "$WEB_UNIT" 0
    runtime_oom "$COLLECTOR_UNIT" 0
    if swapon --show=NAME --noheadings | grep -qx '/dev/zram0'; then
        swapoff /dev/zram0
    fi
    modprobe -r zram 2>/dev/null || true
    status
}

case "${1:-}" in
    --status) status ;;
    --rollback) rollback ;;
    '') apply ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac
