#!/bin/bash
# Generate a static HTML bandwidth report from vnStat data.
# Output: /var/lib/travel-router/bandwidth.html
# Run daily by generate-bandwidth-report.timer.

set -euo pipefail

OUT="/var/lib/travel-router/bandwidth.html"
mkdir -p "$(dirname "$OUT")"

{
printf '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">\n'
printf '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
printf '<title>Travel Router — Bandwidth</title>\n'
printf '<style>\n'
printf 'body{font-family:monospace;background:#0d1117;color:#e6edf3;padding:1.5em;max-width:900px}\n'
printf 'h1{color:#58a6ff}h2{color:#79c0ff;margin-top:1.5em}\n'
printf 'pre{background:#161b22;padding:1em;border:1px solid #30363d;overflow-x:auto}\n'
printf 'p{color:#8b949e}\n'
printf '</style></head><body>\n'
printf '<h1>Travel Router — Bandwidth Report</h1>\n'
printf '<p>Generated: %s</p>\n' "$(date)"
printf '<h2>wlan0 — WiFi Uplink</h2><pre>\n'
vnstat -i wlan0 2>/dev/null || printf '(no data)\n'
printf '</pre>\n'
printf '<h2>uap0 — AP Client Usage</h2><pre>\n'
vnstat -i uap0 2>/dev/null || printf '(no data)\n'
printf '</pre>\n'
# Include any active USB tether interface
for _iface in enx* rndis0 bnep0; do
    if ip link show "$_iface" >/dev/null 2>&1 && vnstat -i "$_iface" >/dev/null 2>&1; then
        printf '<h2>%s — Tether Uplink</h2><pre>\n' "$_iface"
        vnstat -i "$_iface" 2>/dev/null || true
        printf '</pre>\n'
        break
    fi
done
printf '</body></html>\n'
} > "$OUT"

logger "generate-bandwidth-report: wrote %s" "$OUT"
