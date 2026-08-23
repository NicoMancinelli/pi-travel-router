#!/bin/bash
# scripts/schedule-reboot.sh — scheduled daily reboot management
# Usage:
#   schedule-reboot.sh set <HH:MM> [--skip-if-clients]
#   schedule-reboot.sh clear
#   schedule-reboot.sh status
#   schedule-reboot.sh _do-reboot [--skip-if-clients]
set -euo pipefail

CRON_FILE="/etc/cron.d/travel-router-reboot"
SELF="/usr/local/sbin/schedule-reboot.sh"
LOG_TAG="schedule-reboot"

_log() {
    logger -t "${LOG_TAG}" "$*" 2>/dev/null || true
}

cmd="${1:-}"

case "${cmd}" in

  set)
    time_str="${2:-}"
    skip_flag="${3:-}"

    # Validate HH:MM format
    if ! printf '%s' "${time_str}" | grep -qE '^[0-9]{2}:[0-9]{2}$'; then
        printf 'Error: time must be HH:MM format\n' >&2
        exit 1
    fi

    hh="${time_str%%:*}"
    mm="${time_str##*:}"

    # Validate hour and minute ranges (10# forces base-10 so "08" is not octal)
    if [ "$((10#${hh}))" -lt 0 ] || [ "$((10#${hh}))" -gt 23 ]; then
        printf 'Error: hour must be 0-23\n' >&2
        exit 1
    fi
    if [ "$((10#${mm}))" -lt 0 ] || [ "$((10#${mm}))" -gt 59 ]; then
        printf 'Error: minute must be 0-59\n' >&2
        exit 1
    fi

    # Strip leading zeros for cron (cron treats 08 as octal on some systems)
    cron_hh="$((10#${hh}))"
    cron_mm="$((10#${mm}))"

    if [ "${skip_flag}" = "--skip-if-clients" ]; then
        cron_line="${cron_mm} ${cron_hh} * * * root ${SELF} _do-reboot --skip-if-clients"
    else
        cron_line="${cron_mm} ${cron_hh} * * * root ${SELF} _do-reboot"
    fi

    printf '# Managed by schedule-reboot.sh — do not edit manually\n' > "${CRON_FILE}"
    printf '%s\n' "${cron_line}" >> "${CRON_FILE}"
    chmod 644 "${CRON_FILE}"

    _log "Scheduled daily reboot at ${time_str}${skip_flag:+ (skip-if-clients)}"
    printf 'Scheduled daily reboot at %s\n' "${time_str}"
    ;;

  clear)
    if [ -f "${CRON_FILE}" ]; then
        rm -f "${CRON_FILE}"
        _log "Cleared scheduled reboot"
        printf 'Scheduled reboot cleared\n'
    else
        printf 'No scheduled reboot found\n'
    fi
    ;;

  status)
    if [ ! -f "${CRON_FILE}" ]; then
        printf '{"enabled":false,"time":null,"skip_if_clients":false}\n'
        exit 0
    fi

    # Parse the cron line (skip comment lines)
    cron_line=""
    while IFS= read -r line; do
        case "${line}" in
            '#'*|'') continue ;;
            *) cron_line="${line}" ; break ;;
        esac
    done < "${CRON_FILE}"

    if [ -z "${cron_line}" ]; then
        printf '{"enabled":false,"time":null,"skip_if_clients":false}\n'
        exit 0
    fi

    # Extract minute and hour fields (fields 1 and 2)
    cron_mm="$(printf '%s' "${cron_line}" | awk '{print $1}')"
    cron_hh="$(printf '%s' "${cron_line}" | awk '{print $2}')"

    # Zero-pad to HH:MM
    time_out="$(printf '%02d:%02d' "${cron_hh}" "${cron_mm}")"

    # Detect skip-if-clients flag
    if printf '%s' "${cron_line}" | grep -q -- '--skip-if-clients'; then
        skip_out="true"
    else
        skip_out="false"
    fi

    printf '{"enabled":true,"time":"%s","skip_if_clients":%s}\n' \
        "${time_out}" "${skip_out}"
    ;;

  _do-reboot)
    skip_flag="${2:-}"

    if [ "${skip_flag}" = "--skip-if-clients" ]; then
        client_count="$(iw dev uap0 station dump 2>/dev/null | grep -c Station || true)"
        if [ "${client_count}" -gt 0 ]; then
            _log "skipping reboot: ${client_count} clients connected"
            exit 0
        fi
    fi

    _log "initiating scheduled reboot"
    systemctl reboot
    ;;

  *)
    printf 'Usage: %s {set <HH:MM> [--skip-if-clients]|clear|status}\n' "$0" >&2
    exit 1
    ;;

esac
