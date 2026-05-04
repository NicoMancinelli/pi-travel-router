#!/bin/bash
# Apply domain-based split tunnel routing at boot.
# Routes traffic to SPLIT_TUNNEL_DOMAINS through Tailscale; all other traffic direct.
# Called by split-tunnel.service on startup.

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

[[ "${ENABLE_SPLIT_TUNNEL:-0}" = "1" ]] || exit 0
[[ -n "${SPLIT_TUNNEL_DOMAINS:-}" ]] || { logger "split-tunnel: SPLIT_TUNNEL_DOMAINS empty — skipping"; exit 0; }

LOG_TAG="split-tunnel"

# Create ipset for domain IPs (populated by dnsmasq at resolution time)
if ! ipset list vpn_domains >/dev/null 2>&1; then
    ipset create vpn_domains hash:ip timeout 7200 maxelem 65536
    logger -t "$LOG_TAG" "Created ipset vpn_domains"
fi

# Mark packets destined for vpn_domains with fwmark 0x2
if ! iptables -t mangle -C PREROUTING -m set --match-set vpn_domains dst -j MARK --set-mark 0x2 2>/dev/null; then
    iptables -t mangle -A PREROUTING -m set --match-set vpn_domains dst -j MARK --set-mark 0x2
fi

# Routing table 200: default via tailscale0
if ! ip rule show | grep -q "fwmark 0x2 lookup 200"; then
    ip rule add fwmark 0x2 lookup 200 priority 200 2>/dev/null || true
fi
ip route replace default dev tailscale0 table 200 2>/dev/null || true

logger -t "$LOG_TAG" "Split tunnel active — domains: ${SPLIT_TUNNEL_DOMAINS}"
