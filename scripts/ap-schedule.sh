#!/bin/bash
# Disable or re-enable the AP on schedule.
# Usage: ap-schedule.sh disable | ap-schedule.sh enable

set -euo pipefail
# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

ACTION="${1:-}"

# Wait for hostapd to respond to a ping (up to 10s)
_wait_hostapd() {
    for _ in $(seq 1 10); do
        hostapd_cli -p /var/run/hostapd ping 2>/dev/null | grep -q PONG && return 0
        sleep 1
    done
    return 1
}

case "$ACTION" in
    disable)
        _wait_hostapd || { logger -t ap-schedule "hostapd socket not ready, skipping disable"; exit 0; }
        hostapd_cli -p /var/run/hostapd disable 2>/dev/null || true
        if [ -x /usr/local/bin/notify-router.sh ]; then
            /usr/local/bin/notify-router.sh "AP disabled for the night (${AP_DISABLE_TIME:-02:00}–${AP_ENABLE_TIME:-07:00})" low 2>/dev/null || true
        fi
        logger -t ap-schedule "AP disabled"
        ;;
    enable)
        _wait_hostapd || { logger -t ap-schedule "hostapd not ready for enable"; exit 1; }
        hostapd_cli -p /var/run/hostapd -i uap0 enable 2>/dev/null || true
        logger -t ap-schedule "AP enabled"
        ;;
    *)
        printf "Usage: %s disable|enable\n" "$(basename "$0")" >&2
        exit 1
        ;;
esac
