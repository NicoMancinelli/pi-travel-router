#!/usr/bin/env bash
# logger.sh — structured logging helper for travel-router scripts
# Source this file to get log_info, log_warn, log_error, log_debug functions
# Output format: ISO8601 LEVEL [SERVICE] message
# Also writes to journald via logger(1)

_TR_LOG_SERVICE="${TR_LOG_SERVICE:-$(basename "$0" .sh)}"
_TR_LOG_FILE="${TR_LOG_FILE:-/var/log/travel-router/combined.log}"

_tr_log() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local line="${ts} ${level} [${_TR_LOG_SERVICE}] ${msg}"
    # Write to journal
    logger -t "travel-router.${_TR_LOG_SERVICE}" -p "user.${level,,}" "$msg" 2>/dev/null || true
    # Write to combined log (best-effort)
    if [[ -d "$(dirname "$_TR_LOG_FILE")" ]]; then
        printf '%s\n' "$line" >> "$_TR_LOG_FILE" 2>/dev/null || true
    fi
    # Also to stdout if interactive
    [[ -t 1 ]] && printf '%s\n' "$line"
}

log_info()  { _tr_log INFO  "$@"; }
log_warn()  { _tr_log WARN  "$@"; }
log_error() { _tr_log ERROR "$@"; }
log_debug() { [[ "${TR_DEBUG:-0}" == "1" ]] && _tr_log DEBUG "$@" || true; }
