#!/bin/bash
# Apply domain-based split tunnel routing at boot.
# Routes traffic to SPLIT_TUNNEL_DOMAINS through Tailscale; all other traffic direct.
# Called by split-tunnel.service on startup.

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

# H15: best-effort load of the ip_set kernel module; harmless if already loaded
modprobe ip_set 2>/dev/null || true

# H15: graceful exit if ipset is not available (ipset/iptables-legacy may not be installed)
if ! command -v ipset >/dev/null 2>&1; then
    logger -t split-tunnel "ipset not available — split tunnel cannot be applied; skipping"
    exit 0
fi

# H16: teardown helper — removes routing rules and flushes table 200
teardown_split_tunnel() {
    ip rule del fwmark 1 table 200 2>/dev/null || true
    ip route flush table 200 2>/dev/null || true
    ipset destroy travel-split 2>/dev/null || true
    logger -t split-tunnel "Split tunnel torn down"
}

# H16: if split tunnel is disabled, teardown any leftover state and exit
if [ "${ENABLE_SPLIT_TUNNEL:-0}" != "1" ]; then
    teardown_split_tunnel
    exit 0
fi

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
