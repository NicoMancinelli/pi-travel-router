#!/bin/bash
# Monitors Tailscale tunnel health; ntfy.sh alert on daemon down, stale tunnel, or peer loss.

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

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
backend=$(printf '%s' "$ts_json" | jq -r '.BackendState // "unknown"')
if [ "$backend" != "Running" ]; then
    _notify "Tailscale not running (state: $backend)" high
    exit 0
fi

# 3. Stale handshake? Active peers with no handshake in >5 min
now=$(date +%s)
# shellcheck disable=SC2016
stale_peers=$(printf '%s' "$ts_json" | jq -r --argjson now "$now" '
    .Peer // {} | to_entries[] |
    select(.value.Active == true) |
    select(($now - (.value.LastHandshake // 0)) > 300) |
    .value.HostName' 2>/dev/null || true)
if [ -n "$stale_peers" ]; then
    while IFS= read -r peer; do
        [ -n "$peer" ] && _notify "Tailscale stale handshake: $peer" normal
    done <<< "$stale_peers"
fi

# 4. Peer loss: compare to last known peer list
# shellcheck disable=SC2016
current_peers=$(printf '%s' "$ts_json" | jq -c '[.Peer // {} | to_entries[] | .value.HostName] | sort' 2>/dev/null || printf '%s' "[]")
if [ -f "$STATE_FILE" ]; then
    prev_peers=$(cat "$STATE_FILE")
    # shellcheck disable=SC2016
    lost=$(jq -rn --argjson prev "$prev_peers" --argjson curr "$current_peers" \
        '($prev - $curr) | .[]' 2>/dev/null || true)
    if [ -n "$lost" ]; then
        _notify "Tailscale peer lost: $lost" normal
    fi
fi
printf '%s\n' "$current_peers" > "$STATE_FILE"
