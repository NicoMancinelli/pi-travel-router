#!/bin/bash
# usb-share.sh — Travel NAS: share mounted USB storage over SMB (#30)
#
# Shares /media/travel-data (mounted via mount-storage.sh) to AP clients as
# a guest-accessible SMB share. The AP passphrase is the access control —
# the share is never exposed on uplink interfaces (hotel WiFi / tether).
#
# Config in /etc/default/travel-router:
#   ENABLE_USB_SHARE  — flag consumed by install.sh and the TUI
#   USB_SHARE_NAME    — share name shown to clients (default TravelData)
#   USB_SHARE_RO      — 1 = read-only share (default 0)
#
# Usage:
#   usb-share.sh enable    — write smb.conf, enable + start smbd
#   usb-share.sh disable   — stop + disable smbd
#   usb-share.sh status    — print JSON status

set -euo pipefail

DEFAULTS_FILE="${USB_SHARE_DEFAULTS:-/etc/default/travel-router}"
# shellcheck disable=SC1090
[[ -f "${DEFAULTS_FILE}" ]] && source "${DEFAULTS_FILE}"

MOUNT_POINT="${USB_SHARE_PATH:-/media/travel-data}"
SHARE_NAME="${USB_SHARE_NAME:-TravelData}"
SHARE_RO="${USB_SHARE_RO:-0}"
SMB_CONF="${USB_SHARE_SMB_CONF:-/etc/samba/smb.conf}"
AP_GATEWAY="${AP_GATEWAY:-10.3.141.1}"

if ! [[ "${SHARE_NAME}" =~ ^[A-Za-z0-9._-]{1,32}$ ]]; then
    echo "Error: invalid USB_SHARE_NAME '${SHARE_NAME}' (1-32 chars: letters, digits, . _ -)" >&2
    exit 1
fi

write_smb_conf() {
    local read_only="no"
    [[ "${SHARE_RO}" = "1" ]] && read_only="yes"
    mkdir -p "$(dirname "${SMB_CONF}")"
    # Preserve the distro config once so user edits are never silently lost.
    if [[ -f "${SMB_CONF}" && ! -f "${SMB_CONF}.orig" ]] \
        && ! grep -q "Managed by travel-router" "${SMB_CONF}" 2>/dev/null; then
        cp "${SMB_CONF}" "${SMB_CONF}.orig"
    fi
    cat > "${SMB_CONF}" << EOF
# Managed by travel-router usb-share.sh — edits here are overwritten on enable.
[global]
   workgroup = WORKGROUP
   server string = Travel Router NAS
   server role = standalone server
   # Guest-only access: the AP passphrase is the gate.
   map to guest = Bad User
   guest account = nobody
   server min protocol = SMB2
   # Never expose the share on uplink interfaces (hotel WiFi / tether).
   interfaces = lo uap0 usb0 tailscale0
   bind interfaces only = yes
   load printers = no
   printing = bsd
   printcap name = /dev/null
   disable spoolss = yes
   dns proxy = no
   log file = /var/log/samba/log.%m
   max log size = 100

[${SHARE_NAME}]
   path = ${MOUNT_POINT}
   browseable = yes
   guest ok = yes
   read only = ${read_only}
   force user = nobody
   create mask = 0664
   directory mask = 0775
EOF
}

cmd="${1:-}"

case "${cmd}" in
    enable)
        if ! command -v smbd > /dev/null 2>&1; then
            echo "Error: samba is not installed (apt-get install --no-install-recommends samba)" >&2
            exit 1
        fi
        mkdir -p "${MOUNT_POINT}"
        if ! mountpoint -q "${MOUNT_POINT}" 2>/dev/null; then
            echo "Warning: ${MOUNT_POINT} is not a mountpoint — share exposes an empty directory." >&2
            echo "         Mount a drive first: mount-storage.sh mount <device>" >&2
        fi
        write_smb_conf
        if command -v testparm > /dev/null 2>&1; then
            testparm -s "${SMB_CONF}" > /dev/null 2>&1 || {
                echo "Error: generated ${SMB_CONF} failed testparm validation" >&2
                exit 1
            }
        fi
        # NetBIOS is not needed for SMB2 over port 445 — keep nmbd off to save RAM.
        systemctl disable --now nmbd 2>/dev/null || true
        systemctl enable --now smbd
        echo "USB share '${SHARE_NAME}' enabled — connect to smb://${AP_GATEWAY}/${SHARE_NAME}"
        ;;

    disable)
        systemctl disable --now smbd 2>/dev/null || true
        systemctl disable --now nmbd 2>/dev/null || true
        echo "USB share disabled"
        ;;

    status)
        active="false"
        systemctl is-active --quiet smbd 2>/dev/null && active="true"
        mounted="false"
        mountpoint -q "${MOUNT_POINT}" 2>/dev/null && mounted="true"
        ro="false"
        [[ "${SHARE_RO}" = "1" ]] && ro="true"
        printf '{"enabled": %s, "share": "%s", "path": "%s", "read_only": %s, "mounted": %s}\n' \
            "${active}" "${SHARE_NAME}" "${MOUNT_POINT}" "${ro}" "${mounted}"
        ;;

    *)
        echo "Usage: usb-share.sh enable|disable|status" >&2
        exit 1
        ;;
esac
