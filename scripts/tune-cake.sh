#!/bin/bash
# Measure upload speed and apply result as CAKE bandwidth on wlan0.
# Saves result to /var/lib/travel-router/cake-bandwidth.txt for apply-cake.sh.
# Run weekly by tune-cake.timer; also runnable manually: sudo tune-cake.sh

set -euo pipefail

STATE_FILE="/var/lib/travel-router/cake-bandwidth.txt"
LOG_TAG="tune-cake"

if ! command -v speedtest-cli >/dev/null 2>&1; then
    logger -t "$LOG_TAG" "speedtest-cli not installed — skipping"
    exit 0
fi

mkdir -p /var/lib/travel-router

# M16: bind speedtest to the active uplink interface so measurements reflect
# the correct path, not whatever route the kernel picks by default.
UPLINK_IFACE="wlan0"
UPLINK_STATE="/var/lib/travel-router/uplink.state"
if [ -f "$UPLINK_STATE" ]; then
    _candidate=$(cat "$UPLINK_STATE" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$_candidate" ] && ip link show "$_candidate" >/dev/null 2>&1; then
        UPLINK_IFACE="$_candidate"
    fi
fi
if [ "$UPLINK_IFACE" != "wlan0" ]; then
    logger -t "$LOG_TAG" "Active uplink is $UPLINK_IFACE (not wlan0); speedtest will use $UPLINK_IFACE"
fi

logger -t "$LOG_TAG" "Running upload speedtest on $UPLINK_IFACE..."
UPLOAD_MBIT=$(LC_ALL=C speedtest-cli --simple --no-download --interface "$UPLINK_IFACE" 2>/dev/null \
    | awk '/[Uu]pload/{printf "%d", int($2 * 0.9)}') || true

if [[ -z "$UPLOAD_MBIT" || "$UPLOAD_MBIT" -lt 1 ]]; then
    logger -t "$LOG_TAG" "Speedtest failed or returned zero — retaining existing CAKE config"
    exit 0
fi

BANDWIDTH="${UPLOAD_MBIT}mbit"
echo "$BANDWIDTH" > "$STATE_FILE"
logger -t "$LOG_TAG" "Measured upload: ${UPLOAD_MBIT} Mbit/s → CAKE bandwidth = $BANDWIDTH"

if ip link show "$UPLINK_IFACE" >/dev/null 2>&1; then
    tc qdisc replace dev "$UPLINK_IFACE" root cake bandwidth "$BANDWIDTH" besteffort
    logger -t "$LOG_TAG" "Applied $BANDWIDTH to $UPLINK_IFACE"
fi
