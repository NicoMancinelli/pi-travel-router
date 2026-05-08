#!/bin/bash
# OTA commit: mark current boot slot as permanent
set -euo pipefail

SLOT_FILE="/boot/firmware/next-boot-slot"
[ -f "${SLOT_FILE}" ] || SLOT_FILE="/boot/next-boot-slot"
[ -f "${SLOT_FILE}" ] || { echo "No pending OTA slot — nothing to commit"; exit 0; }

SLOT="$(cat "${SLOT_FILE}")"
rm -f "${SLOT_FILE}"
# Write the committed slot to a status file
mkdir -p /var/lib/travel-router
echo "${SLOT}" > /var/lib/travel-router/active-slot
echo "OTA commit: slot ${SLOT} is now permanent"
# Notify via ntfy if configured
if [ -f /etc/default/travel-router ]; then
    # shellcheck source=/dev/null
    source /etc/default/travel-router
    if [ "${ENABLE_NTFY:-0}" = "1" ] && [ -n "${NTFY_TOPIC:-}" ]; then
        curl -sf -H "Title: OTA Update Applied" \
             -d "Router updated to slot ${SLOT} successfully" \
             "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1 || true
    fi
fi
