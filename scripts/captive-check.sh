#!/bin/bash
set -euo pipefail
# Detect captive portal and temporarily pause Tailscale
# Called automatically by wan-watchdog.sh every 60s
# Also run manually after joining a new network: sudo /usr/local/bin/captive-check.sh

LOGFILE="/var/log/wan-watchdog.log"
# N-H6: use a persistent directory so state survives reboots
_STATE_DIR="/var/lib/travel-router"
STATE_FILE="${_STATE_DIR}/captive-portal-active"
mkdir -p "$_STATE_DIR"
# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

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
        # N-H10: use read -ra to avoid word-split and glob issues
        read -ra args <<< "$TAILSCALE_UP_ARGS"
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
    # M2: strip any character that is not alphanumeric, dash, or underscore (dots excluded to prevent path traversal)
    ssid_slug=$(printf '%s' "$current_ssid" | tr -cs 'a-zA-Z0-9_-' '_' | cut -c1-64)
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

    # C3: use a private per-invocation cookie jar; clean up at each return path
    local COOKIE_JAR
    COOKIE_JAR=$(mktemp /tmp/portal-cookies.XXXXXX)

    local portal_html form_action base_url
    if ! portal_html=$(curl -s --max-time 10 --interface wlan0 \
        -L -c "$COOKIE_JAR" "$redirect_url" 2>/dev/null); then
        log "Portal GET failed — cannot attempt auto-login"
        rm -f "$COOKIE_JAR"
        return 1
    fi

    # M1: handle single-quoted and unquoted action attributes
    form_action=$(printf '%s' "$portal_html" \
        | grep -oiE 'action=["\x27]?[^"\x27 >]+' \
        | head -1 \
        | sed "s/action=[\"']//;s/[\"']$//")
    if [ -z "$form_action" ]; then
        log "No form action found — manual login required"
        rm -f "$COOKIE_JAR"
        return 1
    fi

    base_url=$(printf '%s' "$redirect_url" | grep -o 'https\?://[^/]*')
    [ -n "${base_url:-}" ] || { log "Cannot resolve base URL from redirect"; rm -f "$COOKIE_JAR"; return 1; }
    case "$form_action" in
        http*) ;;                                         # absolute, use as-is
        //*) form_action="http:${form_action}" ;;         # protocol-relative
        /*) form_action="${base_url%/}${form_action}" ;;  # path-relative with leading /
        *)  form_action="${base_url%/}/${form_action}" ;; # path-relative without leading /
    esac

    # N-M5: check curl exit code; log and return 1 on failure
    if ! curl -s -o /dev/null --max-time 10 --interface wlan0 \
        -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
        -X POST "$form_action" \
        -d "accept=true&terms=1&submit=Connect&button=Connect" 2>/dev/null; then
        log "Portal POST failed"
        rm -f "$COOKIE_JAR"
        return 1
    fi
    rm -f "$COOKIE_JAR"

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

# Probe connectivity — try two independent endpoints to avoid false positives
# when a single provider is blocked.  We capture both the HTTP status code and
# the redirect URL in one curl call to avoid a second racy round-trip.
#
# Endpoint A: Google generate_204  (returns 204 on clear internet)
# Endpoint B: Mozilla detectportal (returns "success\n" body with 200 on clear internet)
# Endpoint C: Ubuntu connectivity  (returns "NetworkManager is online\n" body with 200)
#
# Decision matrix:
#   204             → clear internet
#   200 + expected body → clear internet
#   000             → no layer-3 connectivity (wlan0 not associated), skip
#   anything else   → captive portal (redirect or 200+wrong body)

_probe() {
    local url="$1" expected_body="$2"
    # Write body to a temp file so we can check it without a second request
    local tmp; tmp=$(mktemp /tmp/captive-probe.XXXXXX)
    trap 'rm -f "$tmp"' RETURN
    local code redirect_url
    code=$(curl -s -w "%{http_code}\n%{redirect_url}" \
        --max-time 6 --interface wlan0 \
        -o "$tmp" "$url" 2>/dev/null)
    redirect_url=$(printf '%s' "$code" | tail -n1)
    code=$(printf '%s' "$code" | head -n1)
    [[ "$redirect_url" == http* ]] || redirect_url=""
    local body; body=$(cat "$tmp" 2>/dev/null); rm -f "$tmp"

    if [ "$code" = "204" ]; then
        echo "clear"; return
    elif [ "$code" = "000" ]; then
        echo "noconn"; return
    elif [ "$code" = "200" ] && [ -n "$expected_body" ]; then
        # Body-match check (strip trailing newline for comparison)
        local trimmed; trimmed=$(printf '%s' "$body" | tr -d '\r\n')
        if [ "$trimmed" = "$expected_body" ]; then
            echo "clear"; return
        fi
    fi
    # Any redirect or unexpected body → portal
    printf '%s\n' "portal ${redirect_url}"
}

# Try endpoint A first
RESULT_A=$(_probe "http://connectivitycheck.gstatic.com/generate_204" "")

case "$RESULT_A" in
    clear)
        PORTAL_RESULT="clear"
        REDIRECT_URL=""
        ;;
    noconn)
        PORTAL_RESULT="noconn"
        REDIRECT_URL=""
        ;;
    *)
        # Ambiguous — try endpoint B before declaring portal
        RESULT_B=$(_probe "http://detectportal.firefox.com/success.txt" "success")
        case "$RESULT_B" in
            clear)
                PORTAL_RESULT="clear"
                REDIRECT_URL=""
                ;;
            noconn)
                # Both endpoints unreachable: no layer-3 connectivity
                PORTAL_RESULT="noconn"
                REDIRECT_URL=""
                ;;
            *)
                # Both probes agree: portal present
                PORTAL_RESULT="portal"
                # Prefer the redirect URL from whichever probe captured one
                REDIRECT_URL=$(printf '%s' "$RESULT_A" | cut -d' ' -f2-)
                [ -z "$REDIRECT_URL" ] && \
                    REDIRECT_URL=$(printf '%s' "$RESULT_B" | cut -d' ' -f2-)
                ;;
        esac
        ;;
esac

if [ "$PORTAL_RESULT" = "clear" ]; then
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
elif [ "$PORTAL_RESULT" = "noconn" ]; then
    log "No layer-3 connectivity on wlan0 — skipping captive portal check"
else
    # Captive portal detected
    if [ ! -f "$STATE_FILE" ]; then
        log "Captive portal detected (redirect='${REDIRECT_URL:-none}') — pausing Tailscale"
        # C2: write state file BEFORE tailscale down so an interrupted run is recoverable
        touch "$STATE_FILE"
        tailscale down 2>/dev/null || true
        if attempt_portal_login "$REDIRECT_URL"; then
            log "Portal auto-login succeeded — restoring Tailscale"
            # N-H11: restore Tailscale immediately after successful portal login
            if restore_tailscale; then
                rm -f "$STATE_FILE"
                logger -t captive-check "Auto-login succeeded, Tailscale restored"
            else
                logger -t captive-check "Tailscale restore failed after portal login — retrying next cycle"
            fi
            notify "Captive portal: auto-login succeeded, Tailscale restored" low
        else
            notify "Captive portal detected! Open browser → authenticate → Tailscale auto-restores" high
            log "Tailscale paused. After portal auth, run: sudo /usr/local/bin/captive-check.sh"
        fi
    fi
fi
