#!/bin/bash
# Called by udev when iPhone tether interface appears (enx*)
# /etc/udev/rules.d/90-ipheth.rules triggers this with %k (interface name)

sleep 3  # wait for ipheth driver + iOS trust handshake
IFACE=$1

logger "start-tether: bringing up $IFACE"
/sbin/dhclient -v "$IFACE" 2>&1 | logger -t "start-tether"

# Prefer tether over wlan0 (metric 100 vs wlan0's 600)
/sbin/ip route change default dev "$IFACE" metric 100 2>/dev/null || true

# Apply CAKE qdisc for bufferbloat control on tether uplink
tc qdisc replace dev "$IFACE" root cake bandwidth 15mbit besteffort 2>/dev/null || true

logger "start-tether: $IFACE configured"
/usr/local/bin/notify-router.sh "iPhone tether connected: $IFACE" low
