#!/bin/bash
# example-accept-terms.sh — template for click-through captive portals
#
# Copy this file to /etc/travel-router/portals/<SSID>.sh and chmod +x it.
# Replace <SSID> with your network name (spaces and / become _).
#
# Usage (called automatically by captive-check.sh):
#   /etc/travel-router/portals/<SSID>.sh "<redirect-url>"
#
# Returns 0 if internet is clear after login, 1 otherwise.

set -euo pipefail

REDIRECT_URL="${1:-}"
COOKIE_JAR="/tmp/portal-cookies-$$.txt"
CONNECT_CHECK="http://connectivitycheck.gstatic.com/generate_204"

cleanup() { rm -f "$COOKIE_JAR"; }
trap cleanup EXIT

# ── Step 1: GET the portal page ───────────────────────────────────────────────
# This loads any session cookies the portal requires before accepting the form.
# --interface wlan0 forces traffic out the uplink rather than Tailscale.
# -L follows any redirects to reach the actual login page.

if [ -z "$REDIRECT_URL" ]; then
    echo "accept-terms portal: no redirect URL — cannot proceed" >&2
    exit 1
fi

portal_html=$(curl -s --max-time 15 --interface wlan0 \
    -L -c "$COOKIE_JAR" "$REDIRECT_URL" 2>/dev/null) || {
    echo "accept-terms portal: GET failed" >&2
    exit 1
}

# ── Step 2: Find the form's POST action URL ───────────────────────────────────
# Most portals embed their action as a relative or absolute URL in a <form>.
# Adjust the grep pattern if this portal uses a different attribute order.

form_action=$(printf '%s' "$portal_html" | grep -oi 'action="[^"]*"' | head -1 | cut -d'"' -f2)

if [ -z "$form_action" ]; then
    echo "accept-terms portal: no form action found — try credentials template or manual login" >&2
    exit 1
fi

# Prepend the base URL if the action is a relative path.
base_url=$(printf '%s' "$REDIRECT_URL" | grep -o 'https\?://[^/]*')
case "$form_action" in
    http*) : ;;                              # already absolute
    /*)    form_action="${base_url}${form_action}" ;;
    *)     form_action="${base_url}/${form_action}" ;;
esac

# ── Step 3: POST the accept-terms fields ─────────────────────────────────────
# These are the most common field names for click-through portals.
# Open DevTools → Network tab in a browser, submit the form, and check the
# POST payload to find the exact names this portal uses, then edit here.

curl -s -o /dev/null --max-time 15 --interface wlan0 \
    -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
    -X POST "$form_action" \
    -d "accept=true&terms=1&button=Connect&submit=Connect&action=accept" \
    2>/dev/null || {
    echo "accept-terms portal: POST failed" >&2
    exit 1
}

# ── Step 4: Wait and verify ───────────────────────────────────────────────────
# Give the portal a moment to process the acceptance before checking.

sleep 2

verify=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 \
    --interface wlan0 "$CONNECT_CHECK" 2>/dev/null)

if [ "$verify" = "204" ]; then
    echo "accept-terms portal: login succeeded" >&2
    exit 0
else
    echo "accept-terms portal: login failed (connectivity check returned HTTP $verify)" >&2
    exit 1
fi
