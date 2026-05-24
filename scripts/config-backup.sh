#!/bin/bash
# scripts/config-backup.sh — backup and restore router configuration as a tar.gz archive
# Usage:
#   config-backup.sh backup [output.tar.gz]
#   config-backup.sh restore <backup.tar.gz>
set -euo pipefail

PROGNAME="$(basename "$0")"

usage() {
    echo "Usage: $PROGNAME backup [output.tar.gz]" >&2
    echo "       $PROGNAME restore <backup.tar.gz>" >&2
    exit 1
}

# ── backup ────────────────────────────────────────────────────────────────────

do_backup() {
    local output="${1:-}"
    if [[ -z "$output" ]]; then
        output="${HOME}/travel-router-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
    fi

    # Build list of paths that exist
    local paths=()
    local always_required=(
        /etc/default/travel-router
        /etc/hostapd/hostapd.conf
    )
    local optional=(
        /etc/wireguard/wg0.conf
        /etc/travel-router
        /etc/dnsmasq.d
        /var/lib/travel-router/active-profile
    )

    for p in "${always_required[@]}"; do
        paths+=("$p")
    done

    for p in "${optional[@]}"; do
        if [[ -e "$p" ]]; then
            paths+=("$p")
        fi
    done

    # Strip leading slash — tar -C / needs relative paths
    local tar_paths=()
    for p in "${paths[@]}"; do
        tar_paths+=("${p#/}")
    done

    tar -czf "$output" -C / "${tar_paths[@]}"
    chmod 600 "$output"

    # Print output path on last line (consumed by the API)
    echo "$output"
}

# ── restore ───────────────────────────────────────────────────────────────────

do_restore() {
    local input="${1:-}"
    if [[ -z "$input" ]]; then
        echo "ERROR: restore requires a backup file argument" >&2
        usage
    fi

    if [[ ! -f "$input" ]]; then
        echo "ERROR: File not found: $input" >&2
        exit 1
    fi

    # Validate: must contain /etc/default/travel-router
    if ! tar -tzf "$input" 2>/dev/null | grep -q "etc/default/travel-router"; then
        echo "ERROR: Archive does not appear to be a valid travel-router backup (missing etc/default/travel-router)" >&2
        exit 1
    fi

    # Back up current config before overwriting
    local pre_backup
    pre_backup="$(mktemp /tmp/travel-router-pre-restore-XXXXXX.tar.gz)"
    echo "Creating pre-restore backup at: $pre_backup" >&2
    do_backup "$pre_backup" >&2 || true   # best-effort

    # Extract to /
    echo "Restoring files from: $input" >&2
    local restored_files
    restored_files="$(tar -xzvf "$input" -C / --no-overwrite-dir 2>&1)"
    echo "$restored_files" >&2

    echo ""
    echo "Restore complete. Files restored:"
    echo "$restored_files"
    echo ""
    echo "WARNING: Reboot or restart services for changes to take effect." >&2
    echo "  sudo systemctl restart hostapd NetworkManager" >&2
    echo "  or: sudo reboot" >&2
}

# ── main ──────────────────────────────────────────────────────────────────────

if [[ $# -lt 1 ]]; then
    usage
fi

cmd="$1"
shift

case "$cmd" in
    backup)
        do_backup "${1:-}"
        ;;
    restore)
        if [[ $# -lt 1 ]]; then
            usage
        fi
        do_restore "$1"
        ;;
    *)
        echo "ERROR: Unknown command: $cmd" >&2
        usage
        ;;
esac
