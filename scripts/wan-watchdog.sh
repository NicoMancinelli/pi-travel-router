#!/bin/bash
# WAN connectivity watchdog with graduated recovery
# Replaces the basic keepalive.sh cron
# Runs every 60s via systemd wan-watchdog.timer

PING_HOST="8.8.8.8"
PING_ALT="1.1.1.1"
LOGFILE="/var/log/wan-watchdog.log"
STATE_FILE="/tmp/wan-watchdog-fails"
NTFY_TOPIC="${NTFY_TOPIC:-}"  # set in /etc/default/travel-router

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOGFILE"; }
notify() { [ -n "$NTFY_TOPIC" ] && curl -s -d "$1" "https://ntfy.sh/$NTFY_TOPIC" > /dev/null 2>&1 || true; }

# Read consecutive fail count
FAILS=$(cat "$STATE_FILE" 2>/dev/null || echo 0)

# Test connectivity (try both targets)
if ping -c 2 -W 3 "$PING_HOST" > /dev/null 2>&1 || ping -c 2 -W 3 "$PING_ALT" > /dev/null 2>&1; then
    if [ "$FAILS" -gt 0 ]; then
        log "WAN restored after $FAILS failure(s)"
        notify "travel-router: WAN restored ✓"
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
        notify "travel-router: WAN down, restarting dhcpcd"
        systemctl restart dhcpcd
        ;;
    3)
        log "Recovery step 3: restarting networking services"
        notify "travel-router: WAN down 3x, restarting networking"
        systemctl restart dhcpcd wpa_supplicant 2>/dev/null || true
        ;;
    4|5)
        log "Recovery step 4-5: waiting..."
        ;;
    *)
        log "Recovery step final: rebooting after $FAILS consecutive failures"
        notify "travel-router: rebooting after $FAILS WAN failures"
        sleep 5
        reboot
        ;;
esac

# Check for captive portal on each run
/usr/local/bin/captive-check.sh &
