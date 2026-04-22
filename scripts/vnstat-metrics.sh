#!/bin/bash
# vnStat → Prometheus textfile exporter
# Writes metrics to /var/lib/prometheus/node-exporter/vnstat.prom
# Optional: push to Prometheus push gateway (set PUSHGW_URL in /etc/default/travel-router)

set -euo pipefail

METRICS_FILE="/var/lib/prometheus/node-exporter/vnstat.prom"
mkdir -p "$(dirname "$METRICS_FILE")"

source /etc/default/travel-router 2>/dev/null || true
PUSHGW_URL="${PUSHGW_URL:-}"

TIMESTAMP=$(date +%s)

vnstat --json d 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
ts = $TIMESTAMP
print('# HELP vnstat_rx_bytes Bytes received per interface today')
print('# TYPE vnstat_rx_bytes gauge')
print('# HELP vnstat_tx_bytes Bytes transmitted per interface today')
print('# TYPE vnstat_tx_bytes gauge')
for iface in data.get('interfaces', []):
    name = iface['name']
    days = iface.get('traffic', {}).get('day', [])
    if days:
        today = days[-1]
        rx = today.get('rx', 0)
        tx = today.get('tx', 0)
        print(f'vnstat_rx_bytes{{interface=\"{name}\"}} {rx} {ts}000')
        print(f'vnstat_tx_bytes{{interface=\"{name}\"}} {tx} {ts}000')
" > "$METRICS_FILE" 2>/dev/null || true

if [ -n "$PUSHGW_URL" ] && [ -f "$METRICS_FILE" ]; then
    curl -s --max-time 10 --data-binary @"$METRICS_FILE" "$PUSHGW_URL" || true
fi
