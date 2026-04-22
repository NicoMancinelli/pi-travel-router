#!/bin/bash
IFACE=$1
logger "stop-tether: $IFACE removed, killing dhclient"
/sbin/dhclient -r "$IFACE" 2>/dev/null || true
/sbin/ip link delete "$IFACE" 2>/dev/null || true
logger "stop-tether: done"
