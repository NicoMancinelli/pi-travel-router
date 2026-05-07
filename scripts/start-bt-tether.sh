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

# H10: check if bt-pan is already running before starting a new instance
if [ -f /run/bt-pan.pid ] && kill -0 "$(cat /run/bt-pan.pid)" 2>/dev/null; then
    logger -t bt-tether "bt-pan already running"
else
    bt-pan --dbus client "$BT_MAC" &
    BT_PAN_PID=$!
    echo "$BT_PAN_PID" > /run/bt-pan.pid
    # N-M26: verify bt-pan process is still alive after 2 seconds (PID not recycled)
    sleep 2
    if ! kill -0 "$BT_PAN_PID" 2>/dev/null; then
        logger -t bt-tether "bt-pan exited immediately (PID $BT_PAN_PID) — aborting"
        rm -f /run/bt-pan.pid
        exit 1
    fi
fi

for _ in $(seq 1 15); do
    ip link show bnep0 >/dev/null 2>&1 && break
    sleep 1
done

if ! ip link show bnep0 >/dev/null 2>&1; then
    logger -t bt-tether "bnep0 did not appear after 15s"
    exit 1
fi

# H10: Bookworm uses nmcli/dhcpcd; dhclient is not present
nmcli device connect bnep0 2>/dev/null || dhcpcd bnep0 2>/dev/null || true

# N-M10: wait for bnep0 to obtain an IP address before proceeding (up to 15 s)
for _ in $(seq 1 15); do
    ip addr show bnep0 2>/dev/null | grep -q 'inet ' && break
    sleep 1
done

ip route del default dev bnep0 2>/dev/null || true
GW=$(ip route show dev bnep0 | awk '/via/{print $3}' | head -1)
[ -n "$GW" ] && ip route add default via "$GW" dev bnep0 metric 300

tc qdisc replace dev bnep0 root cake bandwidth 3mbit besteffort 2>/dev/null || true

/usr/local/bin/failover-watchdog.sh 2>/dev/null || true

logger -t bt-tether "BT PAN up: bnep0 via ${GW:-unknown} metric 300"
/usr/local/bin/notify-router.sh "Bluetooth tether connected: bnep0" 2>/dev/null || true
