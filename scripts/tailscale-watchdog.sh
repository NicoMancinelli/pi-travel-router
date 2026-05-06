#!/bin/bash
# Monitors Tailscale tunnel health; ntfy.sh alert on daemon down, stale tunnel, or peer loss.

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

# H14: jq is required for peer monitoring; exit gracefully if missing
command -v jq >/dev/null 2>&1 || { logger -t tailscale-watchdog "jq not installed — peer monitoring disabled"; exit 0; }

NTFY_TOPIC="${NTFY_TOPIC:-}"
STATE_DIR="/var/lib/travel-router"
STATE_FILE="$STATE_DIR/ts-peers.json"
mkdir -p "$STATE_DIR"

_notify() {
    local msg="$1" priority="${2:-normal}"
    if [ -n "$NTFY_TOPIC" ] && [ -x /usr/local/bin/notify-router.sh ]; then
        /usr/local/bin/notify-router.sh "$msg" "$priority" 2>/dev/null || true
    else
        logger -t tailscale-watchdog "$msg"
    fi
}

# 1. Daemon reachable?
if ! ts_json=$(tailscale status --json 2>/dev/null); then
    _notify "Tailscale daemon unreachable" high
    exit 0
fi

# 2. BackendState == Running?
backend=$(printf '%s' "$ts_json" | jq -r '.BackendState // "unknown"' \
    || { logger -t tailscale-watchdog "jq parse error on BackendState"; exit 1; })
if [ "$backend" != "Running" ]; then
    _notify "Tailscale not running (state: $backend)" high
    exit 0
fi

# 3. Stale handshake? Active peers with no handshake in >5 min
now=$(date +%s)
# shellcheck disable=SC2016
stale_peer=$(printf '%s' "$ts_json" | jq -r --argjson now "$now" '
    .Peer // {} | to_entries[] |
    select(.value.Active == true) |
    select(($now - (.value.LastHandshake // 0)) > 300) |
    .value.HostName' \
    | head -1 \
    || { logger -t tailscale-watchdog "jq parse error on stale-peer check"; exit 1; })
if [ -n "$stale_peer" ]; then
    _notify "Tailscale stale handshake: $stale_peer" normal
fi

# 4. Peer loss: compare to last known peer list
# shellcheck disable=SC2016
current_peers=$(printf '%s' "$ts_json" | jq -c '[.Peer // {} | to_entries[] | .value.HostName] | sort' \
    || { logger -t tailscale-watchdog "jq parse error on peer list"; exit 1; })
if [ -f "$STATE_FILE" ]; then
    prev_peers=$(cat "$STATE_FILE")
    # shellcheck disable=SC2016
    lost=$(jq -rn --argjson prev "$prev_peers" --argjson curr "$current_peers" \
        '($prev - $curr) | .[]' \
        || { logger -t tailscale-watchdog "jq parse error on peer diff"; exit 1; })
    if [ -n "$lost" ]; then
        _notify "Tailscale peer lost: $lost" normal
    fi
fi
printf '%s\n' "$current_peers" > "$STATE_FILE"
