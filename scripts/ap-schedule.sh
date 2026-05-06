#!/bin/bash
# Disable or re-enable the AP on schedule.
# Usage: ap-schedule.sh disable | ap-schedule.sh enable

set -euo pipefail
# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

ACTION="${1:-}"

# M19: verify hostapd is running before issuing cli commands
systemctl is-active --quiet hostapd || {
    /usr/local/bin/notify-router.sh "AP schedule: hostapd not running" high 2>/dev/null || true
    exit 1
}

case "$ACTION" in
    disable)
        hostapd_cli -p /var/run/hostapd disable 2>/dev/null || true
        if [ -x /usr/local/bin/notify-router.sh ]; then
            /usr/local/bin/notify-router.sh "AP disabled for the night (${AP_DISABLE_TIME:-02:00}–${AP_ENABLE_TIME:-07:00})" low 2>/dev/null || true
        fi
        logger -t ap-schedule "AP disabled"
        ;;
    enable)
        hostapd_cli -p /var/run/hostapd enable 2>/dev/null || true
        logger -t ap-schedule "AP enabled"
        ;;
    *)
        printf "Usage: %s disable|enable\n" "$(basename "$0")" >&2
        exit 1
        ;;
esac
