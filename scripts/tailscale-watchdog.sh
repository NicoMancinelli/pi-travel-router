#!/bin/bash
# Monitors Tailscale tunnel health; ntfy.sh alert on daemon down, stale tunnel, or peer loss.

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

# N-L5: prevent concurrent instances from racing
exec 9>/run/lock/tailscale-watchdog.lock
flock -n 9 || exit 0

# H14: jq is required for peer monitoring; exit gracefully if missing
command -v jq >/dev/null 2>&1 || { logger -t tailscale-watchdog "jq not installed — peer monitoring disabled"; exit 0; }

NTFY_TOPIC="${NTFY_TOPIC:-}"
STATE_DIR="/var/lib/travel-router"
# N-M14: configurable stale-handshake threshold (default 300 s)
TS_STALE_HANDSHAKE_SECS="${TS_STALE_HANDSHAKE_SECS:-300}"
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
    # N-M15: exit 1 when daemon is unreachable so callers can detect failure
    exit 1
fi

# 2. BackendState == Running?
if ! backend=$(printf '%s' "$ts_json" | jq -r '.BackendState // "unknown"') || [ -z "$backend" ]; then
    logger -t tailscale-watchdog "jq parse error on BackendState"
    exit 1
fi
if [ "$backend" != "Running" ]; then
    _notify "Tailscale not running (state: $backend)" high
    exit 0
fi

# 3. Stale handshake? Active peers with no handshake in >threshold and TxBytes > 0
now=$(date +%s)
# N-M14: use configurable threshold and only alert on peers with TxBytes > 0
# shellcheck disable=SC2016
if ! stale_peer=$(printf '%s' "$ts_json" | jq -r --argjson now "$now" --argjson thresh "$TS_STALE_HANDSHAKE_SECS" '
    .Peer // {} | to_entries[] |
    select(.value.Active == true) |
    select((.value.TxBytes // 0) > 0) |
    select(($now - (.value.LastHandshake // 0)) > $thresh) |
    .value.HostName' \
    | head -1); then
    logger -t tailscale-watchdog "jq parse error on stale-peer check"
    exit 1
fi
if [ -n "$stale_peer" ]; then
    _notify "Tailscale stale handshake: $stale_peer" normal
fi

# 4. Peer loss: compare to last known peer list
# shellcheck disable=SC2016
if ! current_peers=$(printf '%s' "$ts_json" | jq -c '[.Peer // {} | to_entries[] | .value.HostName] | sort') || [ -z "$current_peers" ]; then
    logger -t tailscale-watchdog "jq parse error on peer list"
    exit 1
fi
if [ -f "$STATE_FILE" ]; then
    prev_peers=$(cat "$STATE_FILE")
    if ! lost=$(jq -rn --argjson prev "$prev_peers" --argjson curr "$current_peers" \
        '($prev - $curr) | .[]'); then
        logger -t tailscale-watchdog "jq parse error on peer diff"
        exit 1
    fi
    if [ -n "$lost" ]; then
        _notify "Tailscale peer lost: $lost" normal
    fi
fi
printf '%s\n' "$current_peers" > "$STATE_FILE"
