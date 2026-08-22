#!/bin/bash
# overlayfs-ctl.sh — read-only root filesystem (overlayfs) control (#10)
#
# With the overlay enabled all writes go to RAM and are lost on reboot — the
# SD card stays read-only, which eliminates corruption from sudden power loss.
# Disable the overlay (and reboot) before anything that must persist:
# update-router.sh, apt upgrade, install.sh, or config edits.
#
# Usage:
#   overlayfs-ctl.sh status    — print JSON: whether the overlay is active
#   overlayfs-ctl.sh enable    — enable overlay (takes effect after reboot)
#   overlayfs-ctl.sh disable   — disable overlay (takes effect after reboot)

set -euo pipefail

CMDLINE="${OVERLAYFS_CMDLINE:-/proc/cmdline}"

overlay_active() {
    grep -qw "boot=overlay" "${CMDLINE}" 2>/dev/null
}

require_raspi_config() {
    command -v raspi-config > /dev/null 2>&1 || {
        echo "Error: raspi-config not found — overlayfs control requires Pi OS" >&2
        exit 1
    }
}

cmd="${1:-}"

case "${cmd}" in
    status)
        if overlay_active; then
            printf '{"active": true, "note": "root is read-only; writes go to RAM and vanish on reboot"}\n'
        else
            printf '{"active": false, "note": "root is read-write (normal mode)"}\n'
        fi
        ;;

    enable)
        if overlay_active; then
            echo "Overlay already active — nothing to do"
            exit 0
        fi
        require_raspi_config
        # raspi-config nonint convention: 0 = enable, 1 = disable.
        # Rebuilds the initramfs — takes a few minutes on a Pi Zero 2 W.
        raspi-config nonint do_overlayfs 0
        echo "Overlay enabled — reboot to activate read-only root"
        echo "NOTE: run 'overlayfs-ctl.sh disable' + reboot before system updates"
        ;;

    disable)
        require_raspi_config
        raspi-config nonint do_overlayfs 1
        if overlay_active; then
            echo "Overlay disabled — reboot to return to read-write root"
        else
            echo "Overlay disabled"
        fi
        ;;

    *)
        echo "Usage: overlayfs-ctl.sh status|enable|disable" >&2
        exit 1
        ;;
esac
