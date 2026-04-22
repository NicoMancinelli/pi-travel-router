#!/bin/bash
# Send push notification via ntfy.sh
# Usage: notify-router.sh "message" [priority]
# Priority: default | low | high | urgent
#
# Set NTFY_TOPIC in /etc/default/travel-router to activate
# Install ntfy app (iOS/Android) and subscribe to your topic
source /etc/default/travel-router 2>/dev/null

MSG="${1:-ping}"
PRIORITY="${2:-default}"

if [ -z "$NTFY_TOPIC" ]; then
    logger "notify-router: NTFY_TOPIC not set in /etc/default/travel-router"
    exit 0
fi

curl -s \
    -H "Priority: $PRIORITY" \
    -H "Title: Travel Router" \
    -d "$MSG" \
    "https://ntfy.sh/$NTFY_TOPIC" > /dev/null 2>&1

logger "notify-router: sent '$MSG'"
