#!/bin/bash
# Bluetooth PAN tethering — connect to paired iPhone, bring up bnep0 at metric 300
# Requires: bluez, bluez-tools
# One-time pairing: see /usr/local/share/travel-router-docs/bluetooth-pair.txt
# Usage: start-bt-tether.sh [BT_MAC]

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true
BT_MAC="${1:-${IPHONE_BT_MAC:-}}"

if [ -z "$BT_MAC" ]; then
    logger -t bt-tether "No IPHONE_BT_MAC set in /etc/default/travel-router"
    exit 1
fi

logger -t bt-tether "Connecting Bluetooth PAN to $BT_MAC"
bt-pan --dbus client "$BT_MAC" &
BT_PAN_PID=$!
trap 'kill "$BT_PAN_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 15); do
    ip link show bnep0 >/dev/null 2>&1 && break
    sleep 1
done

if ! ip link show bnep0 >/dev/null 2>&1; then
    logger -t bt-tether "bnep0 did not appear after 15s"
    exit 1
fi

dhclient -v -timeout 30 bnep0 2>&1 | logger -t bt-tether || true

ip route del default dev bnep0 2>/dev/null || true
GW=""
for _ in $(seq 1 10); do
    GW=$(ip route show default dev bnep0 | awk '{for(i=1;i<=NF;i++){if($i=="via"){print $(i+1);exit}}}')
    [ -n "$GW" ] && break
    sleep 1
done
if [ -n "$GW" ]; then
    ip route add default via "$GW" dev bnep0 metric 300
else
    logger -t bt-tether "WARNING: no gateway found on bnep0 after 10s"
fi

tc qdisc replace dev bnep0 root cake bandwidth 3mbit besteffort 2>/dev/null || true

/usr/local/bin/failover-watchdog.sh 2>/dev/null || true

trap - EXIT  # bt-pan stays running intentionally; clear failure trap on success path
logger -t bt-tether "BT PAN up: bnep0 via ${GW:-unknown} metric 300"
/usr/local/bin/notify-router.sh "Bluetooth tether connected: bnep0" 2>/dev/null || true
