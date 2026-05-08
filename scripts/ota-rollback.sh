#!/bin/bash
# OTA rollback: boot the other slot on next reboot
set -euo pipefail

ACTIVE="$(cat /var/lib/travel-router/active-slot 2>/dev/null || echo "root_a")"
case "${ACTIVE}" in
    root_a) ROLLBACK="root_b" ;;
    root_b) ROLLBACK="root_a" ;;
    *) echo "ERROR: Unknown active slot ${ACTIVE}"; exit 1 ;;
esac

echo "${ROLLBACK}" > /boot/firmware/next-boot-slot 2>/dev/null || \
echo "${ROLLBACK}" > /boot/next-boot-slot

echo "Rollback: will boot ${ROLLBACK} on next reboot"
echo "Run: reboot"
