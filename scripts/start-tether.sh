#!/bin/bash
# Called by udev when iPhone tether interface appears
# Wait for interface to fully initialize
sleep 3
IFACE=$1
logger "start-tether: bringing up $IFACE"
/sbin/dhclient -v "$IFACE" 2>&1 | logger -t "start-tether"
# Set low metric so tether is preferred over wlan0
/sbin/ip route change default dev "$IFACE" metric 100 2>/dev/null || true
logger "start-tether: $IFACE configured"
