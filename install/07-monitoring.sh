#!/bin/bash
# install/07-monitoring.sh — vnStat, Prometheus, ntfy, bandwidth dashboard, UPS
# Defines run_monitoring(). Source this file; do not execute directly.

run_monitoring() {
    # ── vnStat interface init ───────────────────────────────────────────────────
    section "vnStat"

    mkdir -p /var/lib/prometheus/node-exporter
    vnstat --add -i wlan0 2>/dev/null || true
    vnstat --add -i uap0  2>/dev/null || true
    ok "vnStat tracking wlan0 + uap0"
}
