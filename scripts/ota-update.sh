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

# SHA256 verification: try to fetch remote manifest
EXPECTED_SHA=""
SHA_URL="${RELEASE_URL}.sha256"
if curl -sfL "${SHA_URL}" -o "${WORK_DIR}/update.img.xz.sha256" 2>/dev/null; then
    EXPECTED_SHA="$(awk '{print $1}' "${WORK_DIR}/update.img.xz.sha256")"
    echo "Remote SHA256 manifest fetched: ${EXPECTED_SHA}"
else
    echo "WARNING: No SHA256 manifest found at ${SHA_URL}, skipping checksum verification"
fi

# Compute SHA256 of decompressed image before writing
if [ -n "${EXPECTED_SHA}" ]; then
    echo "Computing SHA256 of decompressed image..."
    ACTUAL_SHA="$(xz -dk "${WORK_DIR}/update.img.xz" --stdout | sha256sum | awk '{print $1}')"
    if [ "${ACTUAL_SHA}" != "${EXPECTED_SHA}" ]; then
        echo "ERROR: SHA256 mismatch. Expected ${EXPECTED_SHA}, got ${ACTUAL_SHA}"
        echo "Aborting OTA — inactive slot NOT marked for boot."
        exit 1
    fi
    echo "SHA256 verified OK"
fi

echo "Writing to inactive slot ${INACTIVE_SLOT} (${INACTIVE_DEV})..."
xz -dk "${WORK_DIR}/update.img.xz" --stdout | dd of="${INACTIVE_DEV}" bs=4M status=progress conv=fsync

# Set tryboot flag so next boot tries inactive slot
echo "${INACTIVE_SLOT}" > /boot/firmware/next-boot-slot 2>/dev/null || \
echo "${INACTIVE_SLOT}" > /boot/next-boot-slot || true

echo "OTA update written to ${INACTIVE_SLOT}. Reboot to apply."
echo "  ota-commit will run on first successful boot to make it permanent."
echo "  ota-rollback to revert if something goes wrong."
