#!/bin/bash
# Monitor PiSugar 3 / PiSugar 2 battery and initiate safe shutdown when critical.
# Supports: PiSugar server REST API (localhost:8421) and /sys/class/power_supply sysfs.
# Run every 5 minutes by ups-monitor.timer.

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

[[ "${ENABLE_UPS_MONITOR:-0}" = "1" ]] || exit 0

LOG_TAG="ups-monitor"
THRESHOLD="${UPS_SHUTDOWN_THRESHOLD:-10}"

_get_battery_pct() {
    # Try PiSugar server REST API first
    local pct
    pct=$(curl -sf --max-time 2 "http://localhost:8421/get_battery_percentage" 2>/dev/null \
        | awk -F'"data":' 'NF>1{gsub(/[^0-9.]/,"",$2); printf "%.0f", $2+0}') || true
    [[ "$pct" =~ ^[0-9]+$ ]] && [[ "$pct" -gt 0 ]] && { printf '%s' "$pct"; return; }

    # Fallback: sysfs power_supply
    for _ps in /sys/class/power_supply/pisugar* /sys/class/power_supply/PiSugar*; do
        [[ -f "${_ps}/capacity" ]] && { cat "${_ps}/capacity"; return; }
    done

    # Generic: any battery-type power_supply
    for _ps in /sys/class/power_supply/*; do
        local _type
        _type=$(cat "${_ps}/type" 2>/dev/null || true)
        if [[ "$_type" = "Battery" ]]; then
            cat "${_ps}/capacity" 2>/dev/null && return
        fi
    done

    printf ''
}

LEVEL=$(_get_battery_pct)

if [[ -z "$LEVEL" ]]; then
    # No UPS detected — exit silently (normal when no UPS is attached)
    exit 0
fi

# N-M16: validate API response before acting on it
if ! [[ "$LEVEL" =~ ^[0-9]+$ ]] || [[ "$LEVEL" -lt 0 ]] || [[ "$LEVEL" -gt 100 ]]; then
    logger -t "$LOG_TAG" "WARNING: invalid battery level '$LEVEL' — skipping"
    exit 0
fi

logger -t "$LOG_TAG" "Battery: ${LEVEL}%  (shutdown threshold: ${THRESHOLD}%)"

if [[ "$LEVEL" -le "$THRESHOLD" ]]; then
    # N-L4: hysteresis — only initiate shutdown once per boot
    _SHUTDOWN_FLAG="/run/travel-router/ups-shutdown-initiated"
    if [[ -f "$_SHUTDOWN_FLAG" ]]; then
        logger -t "$LOG_TAG" "Shutdown already initiated — not re-issuing"
        exit 0
    fi
    mkdir -p /run/travel-router
    touch "$_SHUTDOWN_FLAG"
    logger -t "$LOG_TAG" "CRITICAL: battery ${LEVEL}% <= threshold ${THRESHOLD}% — shutting down"
    # H12: synchronous notify (no &) so the message is sent before shutdown;
    # N-M17: wrap notify call with timeout to prevent hang
    timeout 10 /usr/local/bin/notify-router.sh \
        "UPS battery critical: ${LEVEL}% — safe shutdown initiated" 2>/dev/null || true
    sleep 20
    shutdown -h now "UPS battery critical (${LEVEL}%)"
fi
