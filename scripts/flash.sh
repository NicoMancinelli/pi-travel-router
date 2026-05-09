#!/usr/bin/env bash
# flash.sh — download the latest pi-travel-router image and flash it to an SD card.
#
# One-liner usage (macOS / Linux):
#   curl -fsSL https://raw.githubusercontent.com/NicoMancinelli/pi-travel-router/main/scripts/flash.sh | bash
#
# Or clone the repo first and run directly:
#   bash scripts/flash.sh
#   bash scripts/flash.sh --dev /dev/disk4    # skip device prompt

set -euo pipefail

REPO="NicoMancinelli/pi-travel-router"
API="https://api.github.com/repos/${REPO}/releases/latest"
RED='\033[1;31m'; GRN='\033[1;32m'; YEL='\033[1;33m'; BLD='\033[1m'; RST='\033[0m'

banner() { printf "\n${BLD}%s${RST}\n" "$*"; }
ok()     { printf "  ${GRN}✓${RST} %s\n" "$*"; }
warn()   { printf "  ${YEL}!${RST} %s\n" "$*"; }
die()    { printf "  ${RED}✗${RST} %s\n" "$*" >&2; exit 1; }

# ── Parse args ────────────────────────────────────────────────────────────────
TARGET_DEV=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dev) TARGET_DEV="$2"; shift 2 ;;
        --dev=*) TARGET_DEV="${1#--dev=}"; shift ;;
        -h|--help)
            echo "Usage: $0 [--dev /dev/diskN]"
            echo "  --dev   SD card device to flash (skip interactive prompt)"
            exit 0 ;;
        *) die "Unknown argument: $1" ;;
    esac
done

# ── OS check ──────────────────────────────────────────────────────────────────
OS="$(uname -s)"
case "${OS}" in
    Darwin) ;;
    Linux)  ;;
    *) die "Unsupported OS: ${OS}. Use macOS or Linux." ;;
esac

# ── Dependency check ─────────────────────────────────────────────────────────
for cmd in curl xz dd; do
    command -v "${cmd}" &>/dev/null || die "Required command not found: ${cmd}"
done

printf "\n"
printf "${BLD}╔══════════════════════════════════════════════════╗${RST}\n"
printf "${BLD}║    pi-travel-router — SD card flash tool         ║${RST}\n"
printf "${BLD}╚══════════════════════════════════════════════════╝${RST}\n\n"

# ── Fetch latest release ──────────────────────────────────────────────────────
banner "Fetching latest release..."
RELEASE_JSON="$(curl -fsSL "${API}" 2>/dev/null)" \
    || die "Could not reach GitHub API. Check your internet connection."

IMG_URL="$(printf '%s' "${RELEASE_JSON}" | python3 -c "
import sys,json,re
data=json.load(sys.stdin)
assets=data.get('assets',[])
for a in assets:
    if re.search(r'\.img\.xz$', a['name']):
        print(a['browser_download_url'])
        break
" 2>/dev/null)"

TAG="$(printf '%s' "${RELEASE_JSON}" | python3 -c "
import sys,json; print(json.load(sys.stdin).get('tag_name','unknown'))
" 2>/dev/null)"

[ -n "${IMG_URL}" ] || die "No .img.xz found in latest release (${TAG}). Check https://github.com/${REPO}/releases"

IMG_NAME="$(basename "${IMG_URL}")"
ok "Latest release: ${TAG}"
ok "Image: ${IMG_NAME}"

# ── List removable disks ──────────────────────────────────────────────────────
banner "Removable storage devices:"
if [ "${OS}" = "Darwin" ]; then
    diskutil list external physical 2>/dev/null || diskutil list | grep -E "^/dev/disk[0-9]"
else
    lsblk -d -o NAME,SIZE,MODEL,TRAN 2>/dev/null | grep -v "^NAME" | grep -v "sda\b" || lsblk
fi
echo ""

# ── Device selection ──────────────────────────────────────────────────────────
if [ -z "${TARGET_DEV}" ]; then
    printf "${YEL}WARNING: this will ERASE the selected device completely.${RST}\n\n"
    printf "Enter SD card device (e.g. ${BLD}/dev/disk4${RST} on macOS, ${BLD}/dev/sdb${RST} on Linux): "
    read -r TARGET_DEV
fi

TARGET_DEV="${TARGET_DEV%/}"  # strip trailing slash
[ -n "${TARGET_DEV}" ] || die "No device specified."

# Sanity: reject if it looks like a root/system disk
if [ "${OS}" = "Darwin" ]; then
    if diskutil info "${TARGET_DEV}" 2>/dev/null | grep -qiE "Disk Image|internal"; then
        die "Device ${TARGET_DEV} looks like an internal disk. Aborting."
    fi
    DISK_SIZE="$(diskutil info "${TARGET_DEV}" 2>/dev/null | awk '/Disk Size/{print $3, $4}' || echo "unknown")"
else
    if [ "${TARGET_DEV}" = "/dev/sda" ] || [ "${TARGET_DEV}" = "/dev/nvme0n1" ]; then
        die "Device ${TARGET_DEV} looks like a primary disk. Aborting."
    fi
    DISK_SIZE="$(lsblk -d -n -o SIZE "${TARGET_DEV}" 2>/dev/null || echo "unknown")"
fi

ok "Target device: ${TARGET_DEV} (${DISK_SIZE})"

printf "\n${RED}${BLD}This will PERMANENTLY ERASE ${TARGET_DEV}.${RST}\n"
printf "Type ${BLD}yes${RST} to continue: "
read -r CONFIRM
[ "${CONFIRM}" = "yes" ] || die "Aborted."

# ── Download ──────────────────────────────────────────────────────────────────
banner "Downloading image..."
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
IMG_XZ="${WORK_DIR}/${IMG_NAME}"

curl -fL --progress-bar "${IMG_URL}" -o "${IMG_XZ}" \
    || die "Download failed. Check your connection and try again."
ok "Downloaded ${IMG_NAME}"

# ── Checksum verify ───────────────────────────────────────────────────────────
SHA_URL="${IMG_URL}.sha256"
if curl -sfL "${SHA_URL}" -o "${WORK_DIR}/release.sha256" 2>/dev/null; then
    EXPECTED_SHA="$(awk '{print $1}' "${WORK_DIR}/release.sha256")"
    banner "Verifying SHA256..."
    ACTUAL_SHA="$(xz -dk "${IMG_XZ}" --stdout | sha256sum | awk '{print $1}')"
    if [ "${ACTUAL_SHA}" != "${EXPECTED_SHA}" ]; then
        die "SHA256 mismatch! Expected ${EXPECTED_SHA}, got ${ACTUAL_SHA}. File may be corrupt."
    fi
    ok "SHA256 verified"
else
    warn "No .sha256 found for this release — skipping checksum verification"
fi

# ── Unmount ───────────────────────────────────────────────────────────────────
banner "Unmounting ${TARGET_DEV}..."
if [ "${OS}" = "Darwin" ]; then
    diskutil unmountDisk "${TARGET_DEV}" || warn "unmountDisk returned non-zero (may be OK)"
else
    # Linux: unmount all partitions
    for part in "${TARGET_DEV}"?*; do
        umount "${part}" 2>/dev/null || true
    done
fi
ok "Unmounted"

# ── Flash ─────────────────────────────────────────────────────────────────────
banner "Flashing image to ${TARGET_DEV}... (this takes 2–5 minutes)"

# On macOS, /dev/rdiskN is the raw (unbuffered) device — much faster than /dev/diskN
if [ "${OS}" = "Darwin" ]; then
    RAW_DEV="${TARGET_DEV/\/dev\/disk//dev/rdisk}"
else
    RAW_DEV="${TARGET_DEV}"
fi

xz -dk "${IMG_XZ}" --stdout \
    | sudo dd of="${RAW_DEV}" bs=4m status=progress conv=fsync 2>&1 \
    || die "dd failed. Try running the script with sudo, or check the device."

if [ "${OS}" = "Darwin" ]; then
    sudo diskutil eject "${TARGET_DEV}" 2>/dev/null && ok "SD card ejected safely" || true
else
    sync
    ok "Write complete — safe to remove SD card"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
printf "\n${GRN}${BLD}✓ Done! SD card is ready.${RST}\n\n"
printf "  1. Insert SD card into Pi Zero 2W\n"
printf "  2. Connect ${BLD}PWR IN${RST} port to power (outer micro-USB)\n"
printf "  3. Connect ${BLD}USB${RST} port to your laptop (middle micro-USB) — ${BLD}data cable required${RST}\n"
printf "  4. Wait ~60 seconds — a USB Ethernet device will appear on your laptop\n"
printf "  5. Open ${BLD}http://192.168.7.1${RST} in your browser\n\n"
printf "  Trouble? Check: ${BLD}system_profiler SPUSBDataType | grep -i ncm${RST}\n\n"
