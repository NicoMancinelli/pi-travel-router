#!/bin/bash
# Called by udev when iPhone tether interface is removed
IFACE=$1

logger "stop-tether: $IFACE removed, releasing DHCP"
/sbin/dhclient -r "$IFACE" 2>/dev/null || true
/sbin/ip link delete "$IFACE" 2>/dev/null || true
logger "stop-tether: done"

/usr/local/bin/notify-router.sh "iPhone tether disconnected: $IFACE" low
