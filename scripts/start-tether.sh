#!/bin/bash
# Called by udev when a USB tether interface appears (enx*, rndis0, usb0)
# Triggered by udev rules with %k (interface name)

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

IFACE="${1:-}"
if [ -z "$IFACE" ]; then
    logger -t start-tether "No interface argument provided"
    exit 2
fi

if ! ip link show "$IFACE" >/dev/null 2>&1; then
    logger -t start-tether "Interface $IFACE not found"
    exit 1
fi

# N-L1: poll for the interface to become UP or UNKNOWN (some USB gadgets report UNKNOWN)
for _ in $(seq 1 15); do
    ip link show "$IFACE" 2>/dev/null | grep -qE 'state (UP|UNKNOWN)' && break
    sleep 1
done
# (interface may still not be UP if the driver is slow — NM will handle DHCP regardless)

logger "start-tether: bringing up $IFACE"
# Let NetworkManager handle DHCP; explicit connect as fallback if NM hasn't auto-connected
nmcli device connect "$IFACE" 2>&1 | logger -t "start-tether" || true

# N-M9: use configurable bandwidth (default 50mbit) sourced from /etc/default/travel-router
TC_TETHER_BW="${TC_TETHER_BW:-50mbit}"
tc qdisc replace dev "$IFACE" root cake bandwidth "$TC_TETHER_BW" besteffort 2>/dev/null || true

# N-H8: run failover-watchdog outside the udev context so it doesn't block udev
systemd-run --no-block --unit=failover-watchdog-tether /usr/local/bin/failover-watchdog.sh 2>/dev/null || \
    /usr/local/bin/failover-watchdog.sh &

logger "start-tether: $IFACE configured"
/usr/local/bin/notify-router.sh "USB tether connected: $IFACE" low 2>/dev/null || true
