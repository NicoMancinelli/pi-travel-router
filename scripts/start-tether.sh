#!/bin/bash
# Called by udev when iPhone tether interface appears (enx*)
# /etc/udev/rules.d/90-ipheth.rules triggers this with %k (interface name)

set -euo pipefail

IFACE="${1:-}"
if [ -z "$IFACE" ]; then
    logger -t start-tether "No interface argument provided"
    exit 2
fi

if ! ip link show "$IFACE" >/dev/null 2>&1; then
    logger -t start-tether "Interface $IFACE not found"
    exit 1
fi

sleep 3  # wait for ipheth driver + iOS trust handshake

logger "start-tether: bringing up $IFACE"
/sbin/dhclient -v "$IFACE" 2>&1 | logger -t "start-tether"

# Apply CAKE qdisc for bufferbloat control on tether uplink
tc qdisc replace dev "$IFACE" root cake bandwidth 15mbit besteffort 2>/dev/null || true

/usr/local/bin/failover-watchdog.sh

logger "start-tether: $IFACE configured"
/usr/local/bin/notify-router.sh "iPhone tether connected: $IFACE" low
