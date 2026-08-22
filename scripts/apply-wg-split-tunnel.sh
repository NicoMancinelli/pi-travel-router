#!/bin/bash
# apply-wg-split-tunnel.sh — CIDR-based split tunnel routing (#6).
# Routes traffic destined for WG_SPLIT_TUNNEL_CIDRS through the VPN egress
# interface (default tailscale0; set WG_SPLIT_TUNNEL_DEV=wg0 for a WireGuard
# upstream peer); everything else follows the main routing tables.
# Called by wg-split-tunnel.service on startup.
#
# Mechanics (mirrors apply-split-tunnel.sh):
#   ipset hash:net populated from config -> one mangle MARK rule (fwmark 0x3)
#   -> ip rule priority 201 -> secondary routing table 201 via egress device.
# Marks/tables in use elsewhere: 0x64/table 100 (per-device VPN),
# 0x2/table 200 (domain split tunnel).

set -euo pipefail

# Overridable for unit testing.
DEFAULTS_FILE="${TR_DEFAULTS_FILE:-/etc/default/travel-router}"
# shellcheck source=/dev/null
source "$DEFAULTS_FILE" 2>/dev/null || true

LOG_TAG="wg-split-tunnel"
SET_NAME="travel_cidr_subnets"
MARK="0x3"
TABLE="201"
PRIORITY="201"

ENABLE_WG_SPLIT_TUNNEL="${ENABLE_WG_SPLIT_TUNNEL:-0}"
WG_SPLIT_TUNNEL_CIDRS="${WG_SPLIT_TUNNEL_CIDRS:-}"
WG_SPLIT_TUNNEL_DEV="${WG_SPLIT_TUNNEL_DEV:-tailscale0}"

# H15: best-effort module load; harmless if already loaded
modprobe ip_set 2>/dev/null || true

# H15: graceful exit if ipset is unavailable (iptables-legacy hosts)
if ! command -v ipset >/dev/null 2>&1; then
    logger -t "$LOG_TAG" "ipset not available — CIDR split tunnel cannot be applied; skipping"
    exit 0
fi

# Teardown helper: removes mark rule first, then destroys the referenced set,
# then the policy rule and table (order matters: kernel refuses to destroy an
# ipset still referenced by an iptables rule).
teardown() {
    iptables -t mangle -D PREROUTING -m set --match-set "$SET_NAME" dst -j MARK --set-mark "$MARK" 2>/dev/null || true
    ipset destroy "$SET_NAME" 2>/dev/null || true
    ip rule del fwmark "$MARK" table "$TABLE" 2>/dev/null || true
    ip route flush table "$TABLE" 2>/dev/null || true
    logger -t "$LOG_TAG" "CIDR split tunnel torn down"
}

if [ "$ENABLE_WG_SPLIT_TUNNEL" != "1" ]; then
    teardown
    exit 0
fi

[[ -n "$WG_SPLIT_TUNNEL_CIDRS" ]] || { logger -t "$LOG_TAG" "WG_SPLIT_TUNNEL_CIDRS empty — skipping"; exit 0; }

valid_cidr() {
    local cidr="$1" ip IFS='.'
    [[ "$cidr" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]] || return 1
    ip="${cidr%/*}"
    for o in $ip; do (( o <= 255 )) || return 1; done
    return 0
}

# Validate all CIDRs and the egress device BEFORE touching the dataplane so a
# bad config can never leave partial state behind.
for cidr in $WG_SPLIT_TUNNEL_CIDRS; do
    if ! valid_cidr "$cidr"; then
        logger -t "$LOG_TAG" "ERROR: invalid CIDR '$cidr' in WG_SPLIT_TUNNEL_CIDRS — refusing to apply"
        echo "wg-split-tunnel: invalid CIDR '$cidr'" >&2
        exit 1
    fi
done

if ! ip link show "$WG_SPLIT_TUNNEL_DEV" >/dev/null 2>&1; then
    logger -t "$LOG_TAG" "ERROR: egress device '$WG_SPLIT_TUNNEL_DEV' not present — cannot configure"
    echo "wg-split-tunnel: egress device '$WG_SPLIT_TUNNEL_DEV' not present" >&2
    exit 1
fi

# Create/refresh the subnet set from config
if ! ipset list "$SET_NAME" >/dev/null 2>&1; then
    ipset create "$SET_NAME" hash:net maxelem 1024
    logger -t "$LOG_TAG" "Created ipset $SET_NAME"
fi
ipset flush "$SET_NAME"
for cidr in $WG_SPLIT_TUNNEL_CIDRS; do
    ipset add "$SET_NAME" "$cidr" -exist
done

# Mark packets destined for configured subnets
if ! iptables -t mangle -C PREROUTING -m set --match-set "$SET_NAME" dst -j MARK --set-mark "$MARK" 2>/dev/null; then
    iptables -t mangle -A PREROUTING -m set --match-set "$SET_NAME" dst -j MARK --set-mark "$MARK"
fi

# Policy rule + secondary table via egress device
if ! ip rule show | grep -qE "fwmark ${MARK}[[:space:]]+lookup[[:space:]]+${TABLE}([^0-9]|$)"; then
    ip rule add fwmark "$MARK" table "$TABLE" priority "$PRIORITY" 2>/dev/null || true
fi
ip route replace default dev "$WG_SPLIT_TUNNEL_DEV" table "$TABLE"

logger -t "$LOG_TAG" "CIDR split tunnel active via $WG_SPLIT_TUNNEL_DEV — subnets: $WG_SPLIT_TUNNEL_CIDRS"
