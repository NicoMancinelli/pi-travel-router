#!/bin/bash
# mount-storage.sh — USB/SD storage mount manager for pi-travel-router
# Usage:
#   mount-storage.sh list              — print JSON array of block devices
#   mount-storage.sh mount <device>    — mount /dev/<device> to /media/travel-data
#   mount-storage.sh unmount           — unmount /media/travel-data

set -euo pipefail

MOUNT_POINT="/media/travel-data"

cmd="${1:-}"

case "${cmd}" in
    list)
        lsblk -J -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,LABEL,RM 2>/dev/null || echo '{"blockdevices":[]}'
        ;;

    mount)
        device="${2:-}"
        if [[ -z "${device}" ]]; then
            echo "Error: device name required" >&2
            exit 1
        fi
        # Validate device name: only lowercase letters and digits, no slashes
        if ! [[ "${device}" =~ ^[a-z0-9]+$ ]]; then
            echo "Error: invalid device name '${device}'" >&2
            exit 1
        fi
        if [[ ! -b "/dev/${device}" ]]; then
            echo "Error: /dev/${device} is not a block device" >&2
            exit 1
        fi
        mkdir -p "${MOUNT_POINT}"
        mount "/dev/${device}" "${MOUNT_POINT}"
        echo "Mounted /dev/${device} at ${MOUNT_POINT}"
        ;;

    unmount)
        umount "${MOUNT_POINT}" 2>/dev/null || true
        echo "Unmounted ${MOUNT_POINT}"
        ;;

    *)
        echo "Usage: mount-storage.sh list|mount <device>|unmount" >&2
        exit 1
        ;;
esac
