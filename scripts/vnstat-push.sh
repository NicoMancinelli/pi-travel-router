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
    json=$(vnstat --json d 1 -i "$iface" 2>/dev/null) || return 0

    rx_today=$(printf '%s' "$json" \
        | awk -F'"rx":' 'NR==1{split($2,a,","); print a[1]+0}')
    tx_today=$(printf '%s' "$json" \
        | awk -F'"tx":' 'NR==1{split($2,a,","); print a[1]+0}')

    json=$(vnstat --json m 1 -i "$iface" 2>/dev/null) || return 0
    rx_month=$(printf '%s' "$json" \
        | awk -F'"rx":' 'NR==1{split($2,a,","); print a[1]+0}')
    tx_month=$(printf '%s' "$json" \
        | awk -F'"tx":' 'NR==1{split($2,a,","); print a[1]+0}')

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

    if printf '%s\n' "$metrics" | curl -sf --data-binary @- \
        "${PUSHGW_URL}/metrics/job/travel-router/instance/${iface}" >/dev/null 2>&1; then
        logger -t "$LOG_TAG" "pushed stats for $iface"
    else
        logger -t "$LOG_TAG" "push failed for $iface (PUSHGW_URL=$PUSHGW_URL)"
    fi
}

for _iface in wlan0 uap0; do
    _push_iface "$_iface"
done
