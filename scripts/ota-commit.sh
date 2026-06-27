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
# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true
if [ -n "${NTFY_TOPIC:-}" ] && [ -x /usr/local/sbin/notify-router.sh ]; then
    /usr/local/sbin/notify-router.sh "OTA update applied: slot ${SLOT} is now permanent" low 2>/dev/null || true
fi
