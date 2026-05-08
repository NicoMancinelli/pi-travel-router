#!/bin/bash
# Monitors WireGuard tunnel health; alerts on interface down or stale peer handshakes.

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

ENABLE_WIREGUARD="${ENABLE_WIREGUARD:-0}"
WG_INTERFACE="${WG_INTERFACE:-wg0}"

if [[ "$ENABLE_WIREGUARD" != "1" ]]; then
    exit 0
fi

exec 9>/run/lock/wireguard-watchdog.lock
flock -n 9 || exit 0

_notify() {
    local msg="$1" priority="${2:-normal}"
    if [ -n "${NTFY_TOPIC:-}" ] && [ -x /usr/local/bin/notify-router.sh ]; then
        /usr/local/bin/notify-router.sh "$msg" "$priority" 2>/dev/null || true
    else
        logger -t wireguard-watchdog "$msg"
    fi
}

# 1. Interface up?
if ! ip link show "$WG_INTERFACE" >/dev/null 2>&1; then
    _notify "WireGuard: $WG_INTERFACE interface missing — attempting restart" high
    systemctl restart "wg-quick@${WG_INTERFACE}" 2>/dev/null || true
    exit 0
fi

if ! ip link show "$WG_INTERFACE" 2>/dev/null | grep -q "UP"; then
    _notify "WireGuard: $WG_INTERFACE is down — attempting restart" high
    systemctl restart "wg-quick@${WG_INTERFACE}" 2>/dev/null || true
    exit 0
fi

# 2. Any peer configuration present?
wg_output=$(wg show "$WG_INTERFACE" 2>/dev/null || true)
if [[ -z "$wg_output" ]]; then
    _notify "WireGuard: $WG_INTERFACE shows no peer info" normal
    exit 0
fi

# 3. Stale handshake check: alert if any peer's last handshake > 3 minutes ago
while IFS= read -r line; do
    if [[ "$line" =~ ^[[:space:]]*peer:\ ([A-Za-z0-9+/]{43}=)$ ]]; then
        current_peer="${BASH_REMATCH[1]}"
    fi
    if [[ "$line" =~ ^[[:space:]]*latest\ handshake:\ (.+)$ ]]; then
        handshake_str="${BASH_REMATCH[1]}"
        # wg show outputs handshake age as "X seconds ago" or "X minutes, Y seconds ago"
        # Parse seconds ago
        total_secs=0
        if [[ "$handshake_str" =~ ([0-9]+)\ minute ]]; then
            total_secs=$(( total_secs + BASH_REMATCH[1] * 60 ))
        fi
        if [[ "$handshake_str" =~ ([0-9]+)\ second ]]; then
            total_secs=$(( total_secs + BASH_REMATCH[1] ))
        fi
        if [[ "$handshake_str" =~ ([0-9]+)\ hour ]]; then
            total_secs=$(( total_secs + BASH_REMATCH[1] * 3600 ))
        fi
        if [[ $total_secs -gt 180 ]]; then
            logger -t wireguard-watchdog "stale handshake: ${current_peer:-unknown} (${total_secs}s ago)"
        fi
    fi
done <<< "$wg_output"
