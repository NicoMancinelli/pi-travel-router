#!/bin/bash
# Detect captive portal and temporarily pause Tailscale
# Called automatically by wan-watchdog.sh every 60s
# Also run manually after joining a new network: sudo /usr/local/bin/captive-check.sh

LOGFILE="/var/log/wan-watchdog.log"
STATE_FILE="/tmp/captive-portal-active"
source /etc/default/travel-router 2>/dev/null

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOGFILE"; }
notify() { /usr/local/bin/notify-router.sh "$1" "${2:-default}" 2>/dev/null || true; }

# Probe connectivity check endpoint (returns 204 if clear internet, redirect if captive portal)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    --max-time 5 \
    --interface wlan0 \
    "http://connectivitycheck.gstatic.com/generate_204" 2>/dev/null)

if [ "$HTTP_CODE" = "204" ]; then
    # Clear internet — re-enable Tailscale if we paused it
    if [ -f "$STATE_FILE" ]; then
        log "Captive portal cleared — re-enabling Tailscale"
        tailscale up --advertise-routes=10.3.141.0/24 --accept-dns=false 2>/dev/null
        rm -f "$STATE_FILE"
        notify "Internet clear — Tailscale restored ✓" low
    fi
elif [ "$HTTP_CODE" = "000" ]; then
    log "No connectivity (HTTP 000) — skipping captive portal check"
else
    # Redirect detected — captive portal present
    if [ ! -f "$STATE_FILE" ]; then
        log "Captive portal detected (HTTP $HTTP_CODE) — pausing Tailscale"
        tailscale down 2>/dev/null
        touch "$STATE_FILE"
        notify "Captive portal detected! Open browser → authenticate → Tailscale auto-restores" high
        log "Tailscale paused. After portal auth, run: sudo /usr/local/bin/captive-check.sh"
    fi
fi
