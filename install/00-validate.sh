#!/bin/bash
# install/00-validate.sh — pre-flight validation
# Defines run_validate(). Source this file; do not execute directly.

run_validate() {
    section "Pre-flight validation"

    # Must run as root
    [[ $EUID -eq 0 ]] || die "Run as root: sudo bash install/run.sh"

    # Must be run from repo root (scripts/ must exist)
    [[ -f "${REPO}/scripts/wan-watchdog.sh" ]] || \
        die "Run from repo root (scripts/ not found at ${REPO}/scripts/)"

    # Hardware check (warn only — allow running on non-Pi for testing)
    if ! uname -m | grep -qE 'armv7l|aarch64'; then
        warn "Expected armv7l/aarch64 — got $(uname -m) (continuing anyway)"
    fi

    # OS check
    if ! grep -q bookworm /etc/os-release 2>/dev/null; then
        warn "Expected Bookworm — continuing anyway"
    fi

    # Required commands
    local _missing=()
    for _cmd in python3 systemctl apt-get curl git; do
        command -v "$_cmd" &>/dev/null || _missing+=("$_cmd")
    done
    if [[ ${#_missing[@]} -gt 0 ]]; then
        die "Required commands not found: ${_missing[*]}"
    fi

    # Disk space (2 GB minimum)
    local _avail_kb
    _avail_kb=$(df -k / | awk 'NR==2{print $4}')
    if [[ "$_avail_kb" -lt 2097152 ]]; then
        warn "Less than 2 GB free on / ($((${_avail_kb}/1024)) MB available) — install may fail"
    fi

    # Internet connectivity with retry (longer timeout for flaky hotel/airport WiFi)
    local _internet_ok=0
    for _i in 1 2 3; do
        if curl -fsS --max-time 15 https://one.one.one.one/ &>/dev/null; then
            _internet_ok=1; break
        fi
        [[ $_i -lt 3 ]] && sleep 5
    done
    if [[ $_internet_ok -eq 0 ]]; then
        warn "Internet connectivity check failed after 3 tries — some installs may fail"
    fi

    ok "Pre-flight checks passed"
}
