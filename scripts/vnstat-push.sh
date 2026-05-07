#!/bin/bash
# Push vnStat interface stats to a Prometheus push gateway.
# Requires PUSHGW_URL set in /etc/default/travel-router.

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

[[ -n "${PUSHGW_URL:-}" ]] || exit 0

LOG_TAG="vnstat-push"

_push_iface() {
    local iface=$1
    local json rx_today tx_today rx_month tx_month

    # T-M6: single vnstat --json call per interface; parse both daily and monthly
    # data in one Python invocation to avoid four separate subprocess calls.
    json=$(vnstat --json -i "$iface" 2>/dev/null) || return 0
    read -r rx_today tx_today rx_month tx_month < <(printf '%s' "$json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
ifaces = d.get('interfaces', [])
traffic = ifaces[0].get('traffic', {}) if ifaces else {}
days = traffic.get('day', [])
months = traffic.get('month', [])
rx_today = days[-1].get('rx', 0) if days else 0
tx_today = days[-1].get('tx', 0) if days else 0
rx_month = months[-1].get('rx', 0) if months else 0
tx_month = months[-1].get('tx', 0) if months else 0
print(rx_today, tx_today, rx_month, tx_month)
" 2>/dev/null) || { rx_today=0; tx_today=0; rx_month=0; tx_month=0; }

    local metrics
    metrics=$(printf \
'# TYPE router_vnstat_rx_bytes_today gauge
router_vnstat_rx_bytes_today{interface="%s"} %s
# TYPE router_vnstat_tx_bytes_today gauge
router_vnstat_tx_bytes_today{interface="%s"} %s
# TYPE router_vnstat_rx_bytes_month gauge
router_vnstat_rx_bytes_month{interface="%s"} %s
# TYPE router_vnstat_tx_bytes_month gauge
router_vnstat_tx_bytes_month{interface="%s"} %s
' "$iface" "$rx_today" "$iface" "$tx_today" "$iface" "$rx_month" "$iface" "$tx_month")

    # T-H7: redact credentials from PUSHGW_URL before logging
    local _pushgw_log
    _pushgw_log=$(printf '%s' "$PUSHGW_URL" | sed 's|://[^@]*@|://***@|g')
    if printf '%s\n' "$metrics" | curl -sf --data-binary @- \
        "${PUSHGW_URL}/metrics/job/travel-router/instance/${iface}" >/dev/null 2>&1; then
        logger -t "$LOG_TAG" "pushed stats for $iface"
    else
        logger -t "$LOG_TAG" "push failed for $iface (PUSHGW_URL=${_pushgw_log})"
    fi
}

# Build interface list: always include wlan0 and uap0; also add the active uplink
# (T-M7) if it differs from wlan0.
declare -A _seen_ifaces
_iface_list=(wlan0 uap0)
_seen_ifaces[wlan0]=1
_seen_ifaces[uap0]=1

if [[ -f /var/lib/travel-router/uplink.state ]]; then
    _active=$(tr -d '[:space:]' < /var/lib/travel-router/uplink.state 2>/dev/null || true)
    if [[ -n "$_active" && -z "${_seen_ifaces[$_active]+x}" ]]; then
        _iface_list+=("$_active")
        _seen_ifaces[$_active]=1
    fi
fi

for _iface in "${_iface_list[@]}"; do
    _push_iface "$_iface"
done
