#!/bin/bash
# Send push notification via ntfy.sh
# Usage: notify-router.sh "message" [priority]
# Priority: default | low | high | urgent
#
# Set NTFY_TOPIC in /etc/default/travel-router to activate
# Set NTFY_TOKEN for token-based auth (Bearer token)
# Install ntfy app (iOS/Android) and subscribe to your topic

# T-L3: enable strict mode
set -euo pipefail

# shellcheck source=/dev/null
# T-H6: add || true so a missing config file is not fatal
source /etc/default/travel-router 2>/dev/null || true

MSG="${1:-ping}"
PRIORITY="${2:-default}"

if [ -z "$NTFY_TOPIC" ]; then
    logger "notify-router: NTFY_TOPIC not set in /etc/default/travel-router"
    exit 0
fi

# H9: URL-encode the topic to handle special characters safely
topic_enc=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" \
    "$NTFY_TOPIC" 2>/dev/null || printf '%s' "$NTFY_TOPIC")

# T-L2: support optional Bearer token auth
if [ -n "${NTFY_TOKEN:-}" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer ${NTFY_TOKEN}")
else
    AUTH_HEADER=()
fi

# N-M19: use --data-raw to avoid curl treating @ as a file reference
curl -s --max-time 10 \
    -H "Priority: $PRIORITY" \
    -H "Title: Travel Router" \
    "${AUTH_HEADER[@]}" \
    --data-raw "$MSG" \
    "https://ntfy.sh/${topic_enc}" > /dev/null 2>&1

logger "notify-router: sent '$MSG'"
