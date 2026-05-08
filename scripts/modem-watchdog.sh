#!/usr/bin/env bash
# modem-watchdog.sh — detect and configure USB LTE modems via ModemManager
set -euo pipefail
# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

LOG_TAG="modem-watchdog"
# MODEM_METRIC: route metric for wwan0 — between iPhone (100) and Android (200)
# Used by NM dispatcher scripts that source this file; exported for sub-processes
export MODEM_METRIC=150

[[ "${ENABLE_LTE_MODEM:-0}" == "1" ]] || exit 0
command -v mmcli >/dev/null 2>&1 || { logger -t "$LOG_TAG" "mmcli not found; skipping"; exit 0; }

# Find first modem
MODEM_IDX=$(mmcli -L 2>/dev/null | grep -oP '/org/freedesktop/ModemManager1/Modem/\K[0-9]+' | head -1)
if [[ -z "$MODEM_IDX" ]]; then
    logger -t "$LOG_TAG" "No modem detected"
    exit 0
fi

# Get modem state
STATE=$(mmcli -m "$MODEM_IDX" --output-keyvalue 2>/dev/null \
    | grep "modem.generic.state " | awk '{print $NF}')
logger -t "$LOG_TAG" "Modem $MODEM_IDX state: ${STATE}"

# Get signal
RSSI=$(mmcli -m "$MODEM_IDX" --signal-get --output-keyvalue 2>/dev/null \
    | grep "rssi" | awk '{print $NF}' | head -1)
logger -t "$LOG_TAG" "Modem signal RSSI: ${RSSI:-unknown}"

# Check if wwan0 interface is up and has a route
if ip link show wwan0 >/dev/null 2>&1; then
    if ! ip route show default dev wwan0 | grep -q .; then
        logger -t "$LOG_TAG" "wwan0 up but no default route; refreshing NM connection"
        nmcli connection up "${LTE_NM_PROFILE:-lte-modem}" 2>/dev/null || true
    fi
fi
