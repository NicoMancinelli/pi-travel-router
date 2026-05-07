#!/bin/bash
# Called by udev when a USB tether interface appears (enx*, rndis0, usb0)
# Triggered by udev rules with %k (interface name)

set -euo pipefail

IFACE="${1:-}"
if [ -z "$IFACE" ]; then
    logger -t start-tether "No interface argument provided"
    exit 2
fi

if [[ ! "$IFACE" =~ ^(enx[0-9a-f]+|rndis0|usb0)$ ]]; then
    logger -t start-tether "Unexpected interface: $IFACE"
    exit 2
fi

if ! ip link show "$IFACE" >/dev/null 2>&1; then
    logger -t start-tether "Interface $IFACE not found"
    exit 1
fi

sleep 3  # wait for driver init (ipheth iOS trust handshake; RNDIS/CDC-ECM enumeration)

logger "start-tether: bringing up $IFACE"
# Let NetworkManager handle DHCP; explicit connect as fallback if NM hasn't auto-connected
nmcli device connect "$IFACE" 2>&1 | logger -t "start-tether" || true

# Apply CAKE qdisc for bufferbloat control on tether uplink
tc qdisc replace dev "$IFACE" root cake bandwidth 15mbit besteffort 2>/dev/null || true

systemd-run --no-block --unit="failover-watchdog-$$" /usr/local/bin/failover-watchdog.sh 2>/dev/null || \
    /usr/local/bin/failover-watchdog.sh 2>/dev/null || true

logger "start-tether: $IFACE configured"
/usr/local/bin/notify-router.sh "USB tether connected: $IFACE" low 2>/dev/null || true
