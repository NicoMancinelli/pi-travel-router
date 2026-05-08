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
    for _cmd in python3 systemctl apt-get curl; do
        command -v "$_cmd" &>/dev/null || _missing+=("$_cmd")
    done
    if [[ ${#_missing[@]} -gt 0 ]]; then
        die "Required commands not found: ${_missing[*]}"
    fi

    # Internet connectivity (best-effort)
    if ! curl -fsS --max-time 5 https://one.one.one.one/ &>/dev/null; then
        warn "Internet connectivity check failed — some installs may fail"
    fi

    ok "Pre-flight checks passed"
}
