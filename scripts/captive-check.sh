#!/bin/bash
# Detect captive portal and temporarily pause Tailscale
# Called automatically by wan-watchdog.sh every 60s
# Also run manually after joining a new network: sudo /usr/local/bin/captive-check.sh

LOGFILE="/var/log/wan-watchdog.log"
STATE_FILE="/tmp/captive-portal-active"
# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null

if command -v flock >/dev/null 2>&1; then
    exec 9>/run/lock/captive-check.lock
    flock -n 9 || exit 0
fi

TAILSCALE_UP_ARGS="${TAILSCALE_UP_ARGS:---advertise-routes=10.3.141.0/24 --accept-dns=false}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOGFILE"; }
notify() { /usr/local/bin/notify-router.sh "$1" "${2:-default}" 2>/dev/null || true; }

restore_tailscale() {
    local args=()
    if [ -n "$TAILSCALE_UP_ARGS" ]; then
        # shellcheck disable=SC2206
        args=($TAILSCALE_UP_ARGS)
    fi
    tailscale up "${args[@]}" 2>/dev/null
}

# Try to auto-authenticate a captive portal.
# Returns 0 if internet is clear after the attempt, 1 otherwise.
attempt_portal_login() {
    local redirect_url="$1"
    local current_ssid
    current_ssid=$(iwgetid -r wlan0 2>/dev/null || echo "")
    log "Portal auto-login: SSID='$current_ssid' redirect='$redirect_url'"

    # Per-SSID hook: /etc/travel-router/portals/<SSID>.sh  (create for known hotel chains)
    local ssid_slug
    ssid_slug=$(printf '%s' "$current_ssid" | tr ' /' '__')
    local ssid_script="/etc/travel-router/portals/${ssid_slug}.sh"
    if [ -x "$ssid_script" ]; then
        log "Running SSID portal script: $ssid_script"
        if "$ssid_script" "$redirect_url"; then
            log "SSID portal script succeeded"
            return 0
        fi
        log "SSID portal script failed — trying generic"
    fi

    # Generic: GET portal page, find <form action>, POST generic accept-terms fields
    [ -z "$redirect_url" ] && { log "No redirect URL — cannot attempt auto-login"; return 1; }

    local portal_html form_action base_url
    portal_html=$(curl -s --max-time 10 --interface wlan0 \
        -L -c /tmp/portal-cookies.txt "$redirect_url" 2>/dev/null)

    form_action=$(printf '%s' "$portal_html" | grep -oi 'action="[^"]*"' | head -1 | cut -d'"' -f2)
    if [ -z "$form_action" ]; then
        log "No form action found — manual login required"
        return 1
    fi

    base_url=$(printf '%s' "$redirect_url" | grep -o 'https\?://[^/]*')
    [[ "$form_action" != http* ]] && form_action="${base_url}${form_action}"

    curl -s -o /dev/null --max-time 10 --interface wlan0 \
        -b /tmp/portal-cookies.txt -c /tmp/portal-cookies.txt \
        -X POST "$form_action" \
        -d "accept=true&terms=1&submit=Connect&button=Connect" 2>/dev/null

    sleep 2
    local verify
    verify=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
        --interface wlan0 "http://connectivitycheck.gstatic.com/generate_204" 2>/dev/null)
    if [ "$verify" = "204" ]; then
        log "Portal auto-login succeeded"
        return 0
    fi
    log "Portal auto-login failed (post-check returned HTTP $verify)"
    return 1
}

# Probe connectivity check endpoint (returns 204 if clear internet, redirect if captive portal)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    --max-time 5 \
    --interface wlan0 \
    "http://connectivitycheck.gstatic.com/generate_204" 2>/dev/null)

if [ "$HTTP_CODE" = "204" ]; then
    # Clear internet — re-enable Tailscale if we paused it
    if [ -f "$STATE_FILE" ]; then
        log "Captive portal cleared — re-enabling Tailscale"
        if restore_tailscale; then
            rm -f "$STATE_FILE"
            notify "Internet clear — Tailscale restored" low
        else
            log "Tailscale restore failed — keeping captive portal state"
            notify "Internet clear, but Tailscale restore failed" high
        fi
    fi
elif [ "$HTTP_CODE" = "000" ]; then
    log "No connectivity (HTTP 000) — skipping captive portal check"
else
    # Redirect detected — capture the redirect URL for auto-login
    REDIRECT_URL=$(curl -s -o /dev/null -w "%{redirect_url}" \
        --max-time 5 --interface wlan0 \
        "http://connectivitycheck.gstatic.com/generate_204" 2>/dev/null)

    if [ ! -f "$STATE_FILE" ]; then
        log "Captive portal detected (HTTP $HTTP_CODE) — pausing Tailscale"
        tailscale down 2>/dev/null
        touch "$STATE_FILE"
        if attempt_portal_login "$REDIRECT_URL"; then
            notify "Captive portal: auto-login succeeded" low
        else
            notify "Captive portal detected! Open browser → authenticate → Tailscale auto-restores" high
            log "Tailscale paused. After portal auth, run: sudo /usr/local/bin/captive-check.sh"
        fi
    fi
fi
