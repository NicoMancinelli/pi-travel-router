#!/bin/bash
# Called by udev when iPhone tether interface is removed
set -u

IFACE="${1:-}"
if [ -z "$IFACE" ]; then
    logger -t stop-tether "No interface argument provided"
    exit 2
fi

logger "stop-tether: $IFACE removed, releasing DHCP"
/sbin/dhclient -r "$IFACE" 2>/dev/null || true
/sbin/ip link delete "$IFACE" 2>/dev/null || true
logger "stop-tether: done"

/usr/local/bin/notify-router.sh "USB tether disconnected: $IFACE" low
/usr/local/bin/failover-watchdog.sh 2>/dev/null || true
