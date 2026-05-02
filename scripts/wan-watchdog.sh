#!/bin/bash
# WAN connectivity watchdog with graduated recovery
# Replaces the basic keepalive.sh cron
# Runs every 60s via systemd wan-watchdog.timer

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

if command -v flock >/dev/null 2>&1; then
    exec 9>/run/lock/wan-watchdog.lock
    flock -n 9 || exit 0
fi

WAN_PING_TARGETS="${WAN_PING_TARGETS:-1.1.1.1 8.8.8.8}"
LOGFILE="/var/log/wan-watchdog.log"
STATE_FILE="/tmp/wan-watchdog-fails"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOGFILE"; }
notify() { /usr/local/bin/notify-router.sh "$1" "${2:-default}" 2>/dev/null || true; }

can_reach_wan() {
    local target
    for target in $WAN_PING_TARGETS; do
        ping -c 2 -W 3 "$target" > /dev/null 2>&1 && return 0
    done
    return 1
}

# Read consecutive fail count
FAILS=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
case "$FAILS" in
    ''|*[!0-9]*) FAILS=0 ;;
esac

# Test connectivity
if can_reach_wan; then
    if [ "$FAILS" -gt 0 ]; then
        log "WAN restored after $FAILS failure(s)"
        notify "travel-router: WAN restored" low
    fi
    echo 0 > "$STATE_FILE"
    exit 0
fi

FAILS=$((FAILS + 1))
echo "$FAILS" > "$STATE_FILE"
log "WAN unreachable — consecutive failures: $FAILS"

case "$FAILS" in
    1)
        log "Recovery step 1: reassociating wlan0"
        wpa_cli -i wlan0 reassociate 2>/dev/null || true
        ;;
    2)
        log "Recovery step 2: restarting dhcpcd"
        notify "travel-router: WAN down, restarting dhcpcd" high
        systemctl restart dhcpcd
        ;;
    3)
        log "Recovery step 3: restarting networking services"
        notify "travel-router: WAN down 3x, restarting networking" high
        systemctl restart dhcpcd wpa_supplicant 2>/dev/null || true
        ;;
    4|5)
        log "Recovery step 4-5: waiting..."
        ;;
    *)
        log "Recovery step final: rebooting after $FAILS consecutive failures"
        notify "travel-router: rebooting after $FAILS WAN failures" urgent
        sleep 5
        reboot
        ;;
esac

# Check for captive portal on each run
/usr/local/bin/captive-check.sh
