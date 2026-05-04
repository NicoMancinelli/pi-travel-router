#!/bin/bash
# Called by udev when iPhone tether interface is removed
set -u

IFACE="${1:-}"
if [ -z "$IFACE" ]; then
    logger -t stop-tether "No interface argument provided"
    exit 2
fi

logger "stop-tether: $IFACE removed, disconnecting via NM"
nmcli device disconnect "$IFACE" 2>/dev/null || true
logger "stop-tether: done"

/usr/local/bin/notify-router.sh "USB tether disconnected: $IFACE" low
/usr/local/bin/failover-watchdog.sh 2>/dev/null || true
