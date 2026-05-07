#!/bin/bash
# Clone a MAC address to wlan0 before joining a hotel/venue captive portal.
# The portal sees one device regardless of how many clients are behind the Pi.
#
# Usage:
#   sudo clone-mac.sh <XX:XX:XX:XX:XX:XX>   # clone specific MAC
#   sudo clone-mac.sh --restore              # restore permanent/hardware MAC
#   sudo clone-mac.sh --show                 # print current wlan0 MAC
#
# Typical workflow:
#   1. On your laptop, get your MAC: ifconfig en0 | awk '/ether/{print $2}'
#   2. On Pi: sudo clone-mac.sh AA:BB:CC:DD:EE:FF
#   3. Join hotel WiFi / complete portal auth
#   4. Optionally restore: sudo clone-mac.sh --restore

set -euo pipefail

IFACE="wlan0"
LOG="/var/log/wan-watchdog.log"
# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

log()    { echo "$(date '+%Y-%m-%d %H:%M:%S') clone-mac: $1" | tee -a "$LOG"; }
notify() { /usr/local/bin/notify-router.sh "$1" "${2:-default}" 2>/dev/null || true; }
die()    { echo "Error: $1" >&2; exit 1; }

[[ $EUID -ne 0 ]] && die "Run as root: sudo clone-mac.sh <MAC>"

validate_mac() {
    [[ "$1" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] \
        || die "Invalid MAC address: $1  (expected XX:XX:XX:XX:XX:XX)"
}

show_mac() {
    local mac
    mac=$(ip link show "$IFACE" | awk '/link\/ether/{print $2}')
    echo "wlan0 current MAC: $mac"
}

restore_mac() {
    # H13: use -p to restore the permanent/hardware-burned MAC address,
    # not -r which would assign a new random MAC.
    log "Restoring permanent (hardware) MAC on $IFACE"
    # N-H9: stop hostapd before taking wlan0 down to avoid crashing uap0
    local _hostapd_was_running=0
    if systemctl is-active --quiet hostapd 2>/dev/null; then
        _hostapd_was_running=1
        systemctl stop hostapd
    fi
    ip link set "$IFACE" down
    macchanger -p "$IFACE"
    ip link set "$IFACE" up
    [ "$_hostapd_was_running" = "1" ] && systemctl start hostapd
    log "MAC restored (permanent/hardware)"
    notify "wlan0 MAC restored to permanent hardware address" low
}

clone_mac() {
    local target="$1"
    validate_mac "$target"
    local current
    current=$(ip link show "$IFACE" | awk '/link\/ether/{print $2}')
    if [[ "${current,,}" == "${target,,}" ]]; then
        echo "wlan0 already has MAC $target — nothing to do"
        exit 0
    fi
    log "Cloning MAC $target onto $IFACE (was $current)"
    # N-H9: stop hostapd before taking wlan0 down to avoid crashing uap0
    local _hostapd_was_running=0
    if systemctl is-active --quiet hostapd 2>/dev/null; then
        _hostapd_was_running=1
        systemctl stop hostapd
    fi
    ip link set "$IFACE" down
    macchanger -m "$target" "$IFACE"
    ip link set "$IFACE" up
    [ "$_hostapd_was_running" = "1" ] && systemctl start hostapd
    log "MAC cloned: $IFACE → $target"
    # N-M29: persist cloned MAC so it can be re-applied after reboot
    # NOTE: for full persistence at boot, configure a oneshot systemd service
    # or a NetworkManager custom config that reads this file.
    mkdir -p /var/lib/travel-router
    printf '%s\n' "$target" > /var/lib/travel-router/cloned-mac
    log "Cloned MAC saved to /var/lib/travel-router/cloned-mac"
    notify "wlan0 MAC cloned to $target for portal auth" low
    echo ""
    echo "wlan0 MAC is now $target"
    echo "Join the hotel WiFi and complete portal auth."
    echo "To restore: sudo clone-mac.sh --restore"
}

case "${1:-}" in
    --show)    show_mac ;;
    --restore) restore_mac ;;
    "")        die "Usage: sudo clone-mac.sh <XX:XX:XX:XX:XX:XX> | --restore | --show" ;;
    *)         clone_mac "$1" ;;
esac
