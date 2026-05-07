#!/bin/bash
# Disable or re-enable the AP on schedule.
# Usage: ap-schedule.sh disable | ap-schedule.sh enable

set -euo pipefail
# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

ACTION="${1:-}"

# N-L3: verify hostapd control socket exists before calling hostapd_cli
_check_hostapd_socket() {
    [ -S /var/run/hostapd/uap0 ] || {
        logger -t ap-schedule "hostapd socket not found"
        return 1
    }
}

case "$ACTION" in
    disable)
        # M19: verify hostapd is running before issuing cli commands
        if ! systemctl is-active --quiet hostapd 2>/dev/null; then
            /usr/local/bin/notify-router.sh "AP schedule: hostapd not running" high 2>/dev/null || true
            exit 1
        fi
        _check_hostapd_socket || exit 1
        hostapd_cli -p /var/run/hostapd disable 2>/dev/null || true
        if [ -x /usr/local/bin/notify-router.sh ]; then
            /usr/local/bin/notify-router.sh "AP disabled for the night (${AP_DISABLE_TIME:-02:00}–${AP_ENABLE_TIME:-07:00})" low 2>/dev/null || true
        fi
        logger -t ap-schedule "AP disabled"
        ;;
    enable)
        # N-M13: start hostapd if not running instead of exiting with error
        if ! systemctl is-active --quiet hostapd 2>/dev/null; then
            logger -t ap-schedule "hostapd not running — starting it"
            systemctl start hostapd
        fi
        _check_hostapd_socket || exit 1
        hostapd_cli -p /var/run/hostapd enable 2>/dev/null || true
        logger -t ap-schedule "AP enabled"
        ;;
    *)
        printf "Usage: %s disable|enable\n" "$(basename "$0")" >&2
        exit 1
        ;;
esac
