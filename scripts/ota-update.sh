#!/bin/bash
# OTA update: download signed release, verify GPG, write to inactive slot
set -euo pipefail

CURRENT_SLOT="$(grep -oE 'root=/dev/[^ ]+' /proc/cmdline | sed 's|root=/dev/||')"
# root_a is sda2/mmcblk0p2, root_b is sda3/mmcblk0p3 (adjust for Pi)
# Determine inactive slot
case "${CURRENT_SLOT}" in
    *2) INACTIVE_DEV="/dev/mmcblk0p3" ; INACTIVE_SLOT="root_b" ;;
    *3) INACTIVE_DEV="/dev/mmcblk0p2" ; INACTIVE_SLOT="root_a" ;;
    *) echo "ERROR: Cannot determine current slot from ${CURRENT_SLOT}"; exit 1 ;;
esac

REPO_URL="${REPO_URL:-https://github.com/NicoMancinelli/pi-travel-router}"
RELEASE_URL="${1:-}"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

# If no URL given, fetch latest release from GitHub API
if [ -z "${RELEASE_URL}" ]; then
    RELEASE_URL="$(curl -sf "${REPO_URL}/releases/latest" | grep -oE 'https://[^"]+\.img\.xz' | head -1)"
fi
[ -z "${RELEASE_URL}" ] && { echo "ERROR: No release URL found"; exit 1; }

echo "Downloading ${RELEASE_URL}..."
curl -fL "${RELEASE_URL}" -o "${WORK_DIR}/update.img.xz"

# GPG verify if .sig exists
SIG_URL="${RELEASE_URL}.sig"
if curl -sfL "${SIG_URL}" -o "${WORK_DIR}/update.img.xz.sig" 2>/dev/null; then
    gpg --verify "${WORK_DIR}/update.img.xz.sig" "${WORK_DIR}/update.img.xz" || \
        { echo "ERROR: GPG signature verification failed"; exit 1; }
    echo "GPG signature verified OK"
else
    echo "WARNING: No signature found at ${SIG_URL}, proceeding without verification"
fi

echo "Writing to inactive slot ${INACTIVE_SLOT} (${INACTIVE_DEV})..."
xz -dk "${WORK_DIR}/update.img.xz" --stdout | dd of="${INACTIVE_DEV}" bs=4M status=progress conv=fsync

# Set tryboot flag so next boot tries inactive slot
echo "${INACTIVE_SLOT}" > /boot/firmware/next-boot-slot 2>/dev/null || \
echo "${INACTIVE_SLOT}" > /boot/next-boot-slot || true

echo "OTA update written to ${INACTIVE_SLOT}. Reboot to apply."
echo "  ota-commit will run on first successful boot to make it permanent."
echo "  ota-rollback to revert if something goes wrong."
