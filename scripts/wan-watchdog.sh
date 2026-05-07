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
# C1: persist fail count across reboots — /tmp is wiped on each boot
mkdir -p /var/lib/travel-router
STATE_FILE="/var/lib/travel-router/wan-watchdog-fails"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOGFILE"; }
notify() { /usr/local/bin/notify-router.sh "$1" "${2:-default}" 2>/dev/null || true; }

# L1: cap log size when logrotate is not managing this file
truncate_log() {
    local _LOG_TMP
    _LOG_TMP=$(mktemp "${LOGFILE}.tmp.XXXXXX")
    if tail -n 10000 "$LOGFILE" > "$_LOG_TMP"; then
        mv "$_LOG_TMP" "$LOGFILE" || rm -f "$_LOG_TMP"
    else
        rm -f "$_LOG_TMP"
    fi
}
truncate_log

can_reach_wan() {
    # N-H4: primary check via ping; if all pings fail, confirm with dual HTTP probe
    # before declaring WAN down — avoids false reboot when ICMP is blocked.
    local target
    for target in $WAN_PING_TARGETS; do
        ping -c 2 -W 3 "$target" > /dev/null 2>&1 && return 0
    done
    # All pings failed — try HTTP probes before concluding WAN is down
    local code_a
    code_a=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
        "https://www.gstatic.com/generate_204" 2>/dev/null)
    [ "$code_a" = "204" ] && return 0
    local body_b
    body_b=$(curl -s --max-time 5 \
        "https://detectportal.firefox.com/success.txt" 2>/dev/null | tr -d '\r\n')
    [ "$body_b" = "success" ] && return 0
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
    # H17: only run captive-check when wlan0 is the active uplink.
    # Tether interfaces give direct internet — no portal to handle.
    _active_uplink=""
    [ -f /var/lib/travel-router/uplink.state ] && \
        _active_uplink=$(cat /var/lib/travel-router/uplink.state)
    # Fall back to routing table if state file absent
    if [ -z "$_active_uplink" ]; then
        _active_uplink=$(ip route show default 2>/dev/null \
            | awk '/default/{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1);exit}}}' \
            | head -1)
    fi
    if [ "$_active_uplink" = "wlan0" ] || [ -z "$_active_uplink" ]; then
        # Pings succeed even behind a captive portal (the gateway responds).
        # Run the captive-check so portal state is kept up-to-date.
        /usr/local/bin/captive-check.sh
    fi
    exit 0
fi

FAILS=$((FAILS + 1))
echo "$FAILS" > "$STATE_FILE"
log "WAN unreachable — consecutive failures: $FAILS"

case "$FAILS" in
    1)
        log "Recovery step 1: reconnecting wlan0 via NetworkManager"
        nmcli device disconnect wlan0 2>/dev/null || true
        # N-M4: wait for disconnect to settle before reconnecting
        sleep 4
        nmcli device connect wlan0 2>/dev/null || true
        ;;
    2)
        log "Recovery step 2: restarting NetworkManager"
        notify "travel-router: WAN down, restarting NetworkManager" high
        systemctl restart NetworkManager
        ;;
    3)
        log "Recovery step 3: cycling wlan0 link + restarting hostapd"
        notify "travel-router: WAN down 3x, cycling WiFi link" high
        # N-H5: confirm hostapd is stopped before taking wlan0 down
        systemctl stop hostapd && sleep 1
        ip link set wlan0 down 2>/dev/null || true
        sleep 3
        ip link set wlan0 up 2>/dev/null || true
        nmcli device connect wlan0 2>/dev/null || true
        systemctl start hostapd 2>/dev/null || true
        ;;
    4)
        log "Recovery step 4: full NetworkManager + dnsmasq restart"
        systemctl restart NetworkManager
        sleep 5
        systemctl restart dnsmasq 2>/dev/null || true
        ;;
    5)
        log "Recovery step 5: waiting before final reboot"
        notify "travel-router: WAN down 5x, will reboot next cycle" urgent
        ;;
    *)
        log "Recovery step final: rebooting after $FAILS consecutive failures"
        notify "travel-router: rebooting after $FAILS WAN failures" urgent
        sleep 5
        reboot
        ;;
esac

# H17: only run captive-check when wlan0 is the active uplink
_active_uplink_fail=""
[ -f /var/lib/travel-router/uplink.state ] && \
    _active_uplink_fail=$(cat /var/lib/travel-router/uplink.state)
if [ -z "$_active_uplink_fail" ]; then
    _active_uplink_fail=$(ip route show default 2>/dev/null \
        | awk '/default/{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1);exit}}}' \
        | head -1)
fi
if [ "$_active_uplink_fail" = "wlan0" ] || [ -z "$_active_uplink_fail" ]; then
    /usr/local/bin/captive-check.sh
fi
