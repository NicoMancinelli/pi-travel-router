#!/bin/bash
# Disconnect Bluetooth PAN tether
set -euo pipefail

ip link set bnep0 down 2>/dev/null || true
# Kill tracked bt-pan process if running
if [ -f /run/bt-pan.pid ]; then
    kill "$(cat /run/bt-pan.pid)" 2>/dev/null || true
    rm -f /run/bt-pan.pid
fi
bt-pan --dbus disconnect 2>/dev/null || true
logger -t bt-tether "Bluetooth PAN disconnected"
/usr/local/bin/notify-router.sh "Bluetooth tether disconnected" 2>/dev/null || true

# H11: trigger failover so the router picks up the next best uplink
/usr/local/bin/failover-watchdog.sh 2>/dev/null || true
