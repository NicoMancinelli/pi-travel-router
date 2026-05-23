#!/bin/bash
# scripts/aide-check.sh — Run AIDE file integrity check and alert on changes
# Deployed to /usr/local/sbin/aide-check.sh by install/08-security.sh

set -euo pipefail

AIDE_CONF="/etc/aide/aide.conf"
LOG_DIR="/var/log/travel-router"
LOG_FILE="${LOG_DIR}/aide.log"
NOTIFY="/usr/local/sbin/notify-router.sh"

# Ensure log directory exists
mkdir -p "$LOG_DIR"

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

log() { echo "[$(ts)] $*" | tee -a "$LOG_FILE"; }

# If aide is not installed, exit silently
if ! command -v aide > /dev/null 2>&1; then
    exit 0
fi

log "Starting AIDE integrity check"

# Run aide --check; capture output and exit code
aide_output=""
aide_rc=0
aide_output="$(aide --check --config="$AIDE_CONF" 2>&1)" || aide_rc=$?

if [[ "$aide_rc" -ne 0 ]]; then
    # Changes detected (or aide error) — log and alert
    log "AIDE detected changes (exit code ${aide_rc})"

    # Truncate output to 500 chars for the notification
    truncated="${aide_output:0:500}"
    if [[ "${#aide_output}" -gt 500 ]]; then
        truncated="${truncated}... (truncated)"
    fi

    log "AIDE output: ${truncated}"

    if [[ -x "$NOTIFY" ]]; then
        "$NOTIFY" "AIDE integrity check: changes detected — ${truncated}" high
    fi
else
    log "AIDE check passed — no changes detected"
fi

# Always exit 0 so the systemd timer does not enter failed state
exit 0
