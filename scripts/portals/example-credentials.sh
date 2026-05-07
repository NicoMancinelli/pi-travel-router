#!/bin/bash
# example-credentials.sh — template for captive portals requiring username/password
#
# Copy this file to /etc/travel-router/portals/<SSID>.sh and chmod +x it.
# Replace <SSID> with your network name (spaces and / become _).
#
# SECURITY: deployed scripts that contain credentials MUST be chmod 600 so that
# only root can read them.  Example:
#   sudo cp /etc/travel-router/portals/examples/example-credentials.sh \
#       /etc/travel-router/portals/MyHotelSSID.sh
#   sudo chmod 600 /etc/travel-router/portals/MyHotelSSID.sh
#
# Usage (called automatically by captive-check.sh):
#   /etc/travel-router/portals/<SSID>.sh "<redirect-url>"
#
# Returns 0 if internet is clear after login, 1 otherwise.
#
# CREDENTIALS — choose one approach:
#
#   Option A (hardcoded — simple, less secure):
#     Set PORTAL_USER and PORTAL_PASS below.
#
#   Option B (config file — keeps credentials out of this script):
#     Create /etc/travel-router/portals/credentials/<SSID>.conf with:
#       PORTAL_USER="roomnumber"
#       PORTAL_PASS="surname"
#     chmod 600 that file and set CONFIG_FILE below.
#
# Finding the right field names:
#   1. Open the portal in a browser with DevTools → Network tab open.
#   2. Enable "Preserve log", then submit the login form.
#   3. Find the POST request; inspect its Form Data / Payload section.
#   4. Replace the -d fields below with the exact names from that payload.

set -euo pipefail

# M21: jq is used below for URL-encoding credentials; fail fast if missing.
# Alternative without jq: python3 -c "import urllib.parse; print(urllib.parse.quote('...'))"
command -v jq >/dev/null 2>&1 || {
    echo "jq required for URL encoding — install with: apt-get install jq" >&2
    exit 1
}

REDIRECT_URL="${1:-}"
COOKIE_JAR="/tmp/portal-cookies-$$.txt"
CONNECT_CHECK="http://connectivitycheck.gstatic.com/generate_204"

# ── Credentials ───────────────────────────────────────────────────────────────
# Option A: hardcode here (chmod 600 /etc/travel-router/portals/<SSID>.sh)
PORTAL_USER=""
PORTAL_PASS=""

# Option B: load from a separate config file
CONFIG_FILE=""   # e.g. /etc/travel-router/portals/credentials/MyHotel.conf
if [ -n "$CONFIG_FILE" ] && [ -r "$CONFIG_FILE" ]; then
    # shellcheck source=/dev/null
    . "$CONFIG_FILE"
fi

if [ -z "$PORTAL_USER" ] || [ -z "$PORTAL_PASS" ]; then
    echo "credentials portal: PORTAL_USER/PORTAL_PASS not set — edit the script" >&2
    exit 1
fi
# ─────────────────────────────────────────────────────────────────────────────

# shellcheck disable=SC2329
cleanup() { rm -f "$COOKIE_JAR"; }
trap cleanup EXIT

# ── Step 1: GET the portal login page ────────────────────────────────────────
# Loads the session and any CSRF tokens embedded in the form.

if [ -z "$REDIRECT_URL" ]; then
    echo "credentials portal: no redirect URL — cannot proceed" >&2
    exit 1
fi

# I-M6: use the active uplink interface instead of hardcoded wlan0
_UPLINK=$(cat /var/lib/travel-router/uplink.state 2>/dev/null || \
    ip route show default 2>/dev/null | awk '/default/{print $5; exit}')
_UPLINK="${_UPLINK:-wlan0}"

portal_html=$(curl -s --max-time 15 --interface "${_UPLINK}" \
    -L -c "$COOKIE_JAR" "$REDIRECT_URL" 2>/dev/null) || {
    echo "credentials portal: GET failed" >&2
    exit 1
}

# ── Step 2: Find the form's POST action URL ───────────────────────────────────
form_action=$(printf '%s' "$portal_html" | grep -oi 'action="[^"]*"' | head -1 | cut -d'"' -f2)

if [ -z "$form_action" ]; then
    echo "credentials portal: no form action found" >&2
    exit 1
fi

base_url=$(printf '%s' "$REDIRECT_URL" | grep -o 'https\?://[^/]*')
case "$form_action" in
    http*) : ;;
    /*)    form_action="${base_url}${form_action}" ;;
    *)     form_action="${base_url}/${form_action}" ;;
esac

# ── Step 3: (Optional) extract a CSRF token ───────────────────────────────────
# Many portals embed a hidden CSRF token in the form.  If the POST returns a
# "token mismatch" or the connectivity check keeps failing, uncomment and adapt:
#
# csrf=$(printf '%s' "$portal_html" | grep -oi 'name="_token" value="[^"]*"' | \
#     head -1 | cut -d'"' -f4)
# # Then add "&_token=${csrf}" to the POST data below.

# ── Step 4: POST the login credentials ───────────────────────────────────────
# These field names are generic placeholders.  Inspect the actual form fields
# (DevTools → Network → POST payload) and update them for your portal.

curl -s -o /dev/null --max-time 15 --interface "${_UPLINK}" \
    -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
    -X POST "$form_action" \
    -d "username=$(printf '%s' "$PORTAL_USER" | jq -sRr @uri)&password=$(printf '%s' "$PORTAL_PASS" | jq -sRr @uri)" \
    2>/dev/null || {
    echo "credentials portal: POST failed" >&2
    exit 1
}

# ── Step 5: Wait and verify ───────────────────────────────────────────────────
sleep 2

verify=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 \
    --interface "${_UPLINK}" "$CONNECT_CHECK" 2>/dev/null)

if [ "$verify" = "204" ]; then
    echo "credentials portal: login succeeded" >&2
    exit 0
else
    echo "credentials portal: login failed (connectivity check returned HTTP $verify)" >&2
    exit 1
fi
