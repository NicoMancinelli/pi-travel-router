#!/bin/bash
# Send push notification via ntfy.sh
# Usage: notify-router.sh "message" [priority]
# Priority: default | low | high | urgent
#
# Set NTFY_TOPIC in /etc/default/travel-router to activate
# Install ntfy app (iOS/Android) and subscribe to your topic

set -u

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

NTFY_TOPIC="${NTFY_TOPIC:-}"
MSG="${1:-ping}"
PRIORITY="${2:-default}"

case "$PRIORITY" in
    default|low|min|high|max|urgent) ;;
    *) PRIORITY="default" ;;
esac

SEVERITY="${NOTIFY_SEVERITY:-info}"
# Map severity to ntfy priority
case "$SEVERITY" in
    critical) NTFY_PRIORITY="urgent" ;;
    warning)  NTFY_PRIORITY="high" ;;
    *)        NTFY_PRIORITY="${NTFY_PRIORITY:-$PRIORITY}" ;;
esac
PRIORITY="$NTFY_PRIORITY"

if [ -z "$NTFY_TOPIC" ]; then
    logger "notify-router: NTFY_TOPIC not set in /etc/default/travel-router"
    exit 0
fi

if [[ ! "$NTFY_TOPIC" =~ ^[A-Za-z0-9_-]+$ ]]; then
    logger -t notify-router "NTFY_TOPIC contains invalid characters, skipping"
    exit 0
fi

if ! curl -sf --max-time 10 --connect-timeout 5 \
    -H "Priority: $PRIORITY" \
    -H "Title: Travel Router" \
    -d "$MSG" \
    "https://ntfy.sh/$NTFY_TOPIC" > /dev/null 2>&1; then
    logger "notify-router: curl failed for '$MSG'"
    exit 1
fi
logger "notify-router: sent '$MSG'"
