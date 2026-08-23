#!/usr/bin/env bats
# Unit tests for scripts/update-router.sh.
# Tests focus on safe allowlisted OTA file installation.

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"

setup() {
    export TEST_ROOT
    TEST_ROOT="$(mktemp -d)"
    export UPDATE_ROUTER_BIN_DIR="${TEST_ROOT}/bin"
    export UPDATE_ROUTER_SBIN_DIR="${TEST_ROOT}/sbin"
    export UPDATE_ROUTER_PORTAL_EXAMPLES_DIR="${TEST_ROOT}/portals/examples"
    export UPDATE_ROUTER_SYSTEMD_DIR="${TEST_ROOT}/systemd"
    export UPDATE_ROUTER_SHARE_DIR="${TEST_ROOT}/share"
    export UPDATE_ROUTER_LIB_DIR="${TEST_ROOT}/lib"
    mkdir -p "$UPDATE_ROUTER_BIN_DIR" "$UPDATE_ROUTER_SBIN_DIR" "$UPDATE_ROUTER_SYSTEMD_DIR" "$UPDATE_ROUTER_SHARE_DIR"
}

teardown() {
    rm -rf "$TEST_ROOT"
}

_load_update_router() {
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/scripts/update-router.sh"
    log() { printf '%s\n' "$*" >> "${TEST_ROOT}/update.log"; }
}

@test "apply_update installs allowlisted Python TUI during OTA" {
    _load_update_router
    src="${TEST_ROOT}/src"
    mkdir -p "${src}/scripts"
    printf '%s\n' 'print("new tui")' > "${src}/scripts/travel-tui.py"
    printf '%s\n' 'print("not allowlisted")' > "${src}/scripts/not-allowlisted.py"
    printf '%s\n' '#!/bin/bash' 'echo status' > "${src}/scripts/travel-status.sh"
    printf '%s\n' '#!/bin/bash' 'echo install' > "${src}/install.sh"

    printf '%s\n' 'print("old tui")' > "${UPDATE_ROUTER_SBIN_DIR}/travel-tui.py"
    printf '%s\n' '#!/bin/bash' 'echo old status' > "${UPDATE_ROUTER_BIN_DIR}/travel-status.sh"
    printf '%s\n' '#!/bin/bash' 'echo old install' > "${UPDATE_ROUTER_SHARE_DIR}/install.sh"

    changed=0
    run apply_update "$src"

    [ "$status" -eq 0 ]
    [ "$(cat "${UPDATE_ROUTER_SBIN_DIR}/travel-tui.py")" = 'print("new tui")' ]
    [ -x "${UPDATE_ROUTER_SBIN_DIR}/travel-tui.py" ]
    [ -x "${UPDATE_ROUTER_SBIN_DIR}/travel-tui" ]
    [ ! -e "${UPDATE_ROUTER_SBIN_DIR}/not-allowlisted.py" ]
    grep -q "updated TUI script: travel-tui.py" "${TEST_ROOT}/update.log"
    grep -q "updated TUI wrapper" "${TEST_ROOT}/update.log"
}

@test "apply_update installs TUI fallback in sbin path used by wrapper" {
    _load_update_router

    src="${TEST_ROOT}/src"
    mkdir -p "${src}/scripts"
    printf '%s\n' '#!/bin/bash' 'echo new legacy' > "${src}/scripts/travel-tui-legacy.sh"
    printf '%s\n' '#!/bin/bash' 'echo install' > "${src}/install.sh"

    printf '%s\n' '#!/bin/bash' 'echo old legacy' > "${UPDATE_ROUTER_SBIN_DIR}/travel-tui-legacy"
    printf '%s\n' '#!/bin/bash' 'echo old install' > "${UPDATE_ROUTER_SHARE_DIR}/install.sh"

    changed=0
    run apply_update "$src"

    [ "$status" -eq 0 ]
    grep -q "new legacy" "${UPDATE_ROUTER_SBIN_DIR}/travel-tui-legacy"
    [ -x "${UPDATE_ROUTER_SBIN_DIR}/travel-tui-legacy" ]
    [ -x "${UPDATE_ROUTER_SBIN_DIR}/travel-tui" ]
    [ ! -e "${UPDATE_ROUTER_BIN_DIR}/travel-tui-legacy.sh" ]
    grep -q "updated TUI fallback: travel-tui-legacy.sh" "${TEST_ROOT}/update.log"
}

@test "apply_update installs OTA scripts in sbin and command aliases in bin" {
    _load_update_router

    src="${TEST_ROOT}/src"
    mkdir -p "${src}/scripts"
    printf '%s\n' '#!/bin/bash' 'echo ota update' > "${src}/scripts/ota-update.sh"
    printf '%s\n' '#!/bin/bash' 'echo ota commit' > "${src}/scripts/ota-commit.sh"
    printf '%s\n' '#!/bin/bash' 'echo ota rollback' > "${src}/scripts/ota-rollback.sh"
    printf '%s\n' '#!/bin/bash' 'echo install' > "${src}/install.sh"

    printf '%s\n' '#!/bin/bash' 'echo old update' > "${UPDATE_ROUTER_SBIN_DIR}/ota-update"
    printf '%s\n' '#!/bin/bash' 'echo old install' > "${UPDATE_ROUTER_SHARE_DIR}/install.sh"

    changed=0
    run apply_update "$src"

    [ "$status" -eq 0 ]
    grep -q "ota update" "${UPDATE_ROUTER_SBIN_DIR}/ota-update"
    grep -q "ota commit" "${UPDATE_ROUTER_SBIN_DIR}/ota-commit"
    grep -q "ota rollback" "${UPDATE_ROUTER_SBIN_DIR}/ota-rollback"
    [ -x "${UPDATE_ROUTER_SBIN_DIR}/ota-update" ]
    [ "$(readlink "${UPDATE_ROUTER_BIN_DIR}/update-router")" = "update-router.sh" ]
    [ "$(readlink "${UPDATE_ROUTER_BIN_DIR}/travel-status")" = "travel-status.sh" ]
    grep -q "updated OTA script: ota-update.sh" "${TEST_ROOT}/update.log"
}

@test "apply_update installs allowlisted sbin scripts during OTA" {
    _load_update_router

    src="${TEST_ROOT}/src"
    mkdir -p "${src}/scripts"
    printf '%s\n' '#!/bin/bash' 'echo new share' > "${src}/scripts/usb-share.sh"
    printf '%s\n' '#!/bin/bash' 'echo new mount' > "${src}/scripts/mount-storage.sh"
    printf '%s\n' '#!/bin/bash' 'echo install' > "${src}/install.sh"

    printf '%s\n' '#!/bin/bash' 'echo old share' > "${UPDATE_ROUTER_SBIN_DIR}/usb-share.sh"

    changed=0
    run apply_update "$src"

    [ "$status" -eq 0 ]
    grep -q "new share" "${UPDATE_ROUTER_SBIN_DIR}/usb-share.sh"
    [ -x "${UPDATE_ROUTER_SBIN_DIR}/usb-share.sh" ]
    grep -q "new mount" "${UPDATE_ROUTER_SBIN_DIR}/mount-storage.sh"
    grep -q "updated sbin script: usb-share.sh" "${TEST_ROOT}/update.log"
}

@test "apply_update does not install non-allowlisted scripts to sbin" {
    _load_update_router

    src="${TEST_ROOT}/src"
    mkdir -p "${src}/scripts"
    printf '%s\n' '#!/bin/bash' 'echo evil' > "${src}/scripts/evil-script.sh"
    printf '%s\n' '#!/bin/bash' 'echo install' > "${src}/install.sh"

    changed=0
    run apply_update "$src"

    [ "$status" -eq 0 ]
    [ ! -e "${UPDATE_ROUTER_SBIN_DIR}/evil-script.sh" ]
}

@test "apply_update skips unchanged sbin scripts" {
    _load_update_router

    src="${TEST_ROOT}/src"
    mkdir -p "${src}/scripts"
    printf '%s\n' '#!/bin/bash' 'echo same' > "${src}/scripts/usb-share.sh"
    printf '%s\n' '#!/bin/bash' 'echo install' > "${src}/install.sh"
    printf '%s\n' '#!/bin/bash' 'echo same' > "${UPDATE_ROUTER_SBIN_DIR}/usb-share.sh"

    changed=0
    run apply_update "$src"

    [ "$status" -eq 0 ]
    ! grep -q "updated sbin script: usb-share.sh" "${TEST_ROOT}/update.log"
}

@test "apply_update installs wireguard and cidr split tunnel scripts during OTA" {
    # Regression: both scripts ship via install.sh but were missing from
    # SCRIPT_ALLOWLIST, so OTA silently skipped them on installed Pis.
    _load_update_router

    src="${TEST_ROOT}/src"
    mkdir -p "${src}/scripts"
    printf '%s\n' '#!/bin/bash' 'echo new wg watchdog' > "${src}/scripts/wireguard-watchdog.sh"
    printf '%s\n' '#!/bin/bash' 'echo new wg split tunnel' > "${src}/scripts/apply-wg-split-tunnel.sh"
    printf '%s\n' '#!/bin/bash' 'echo install' > "${src}/install.sh"

    changed=0
    run apply_update "$src"

    [ "$status" -eq 0 ]
    grep -q "new wg watchdog" "${UPDATE_ROUTER_BIN_DIR}/wireguard-watchdog.sh"
    grep -q "new wg split tunnel" "${UPDATE_ROUTER_BIN_DIR}/apply-wg-split-tunnel.sh"
    grep -q "updated script: wireguard-watchdog.sh" "${TEST_ROOT}/update.log"
    grep -q "updated script: apply-wg-split-tunnel.sh" "${TEST_ROOT}/update.log"
}

@test "apply_update installs phased-installer sbin and bin scripts during OTA" {
    # Regression: these ship via install/ phases at provision time but were
    # missing from allowlists, so OTA silently froze them on installed Pis.
    _load_update_router

    src="${TEST_ROOT}/src"
    mkdir -p "${src}/scripts"
    printf '%s\n' '#!/bin/bash' 'echo new key rotate' > "${src}/scripts/wg-key-rotate.sh"
    printf '%s\n' '#!/bin/bash' 'echo new peer expire' > "${src}/scripts/wg-peer-expire.sh"
    printf '%s\n' '#!/bin/bash' 'echo new aide' > "${src}/scripts/aide-check.sh"
    printf '%s\n' '#!/bin/bash' 'echo new log rotate' > "${src}/scripts/log-rotate.sh"
    printf '%s\n' '#!/bin/bash' 'echo new modem watchdog' > "${src}/scripts/modem-watchdog.sh"

    changed=0
    run apply_update "$src"

    [ "$status" -eq 0 ]
    grep -q "new key rotate" "${UPDATE_ROUTER_SBIN_DIR}/wg-key-rotate.sh"
    grep -q "new peer expire" "${UPDATE_ROUTER_SBIN_DIR}/wg-peer-expire.sh"
    grep -q "new aide" "${UPDATE_ROUTER_SBIN_DIR}/aide-check.sh"
    grep -q "new log rotate" "${UPDATE_ROUTER_BIN_DIR}/log-rotate.sh"
    grep -q "new modem watchdog" "${UPDATE_ROUTER_BIN_DIR}/modem-watchdog.sh"
}

@test "apply_update delivers shared libs to the travel-router lib dir" {
    _load_update_router

    src="${TEST_ROOT}/src"
    mkdir -p "${src}/scripts"
    printf '%s\n' '# shared constants' '_TR_PROBE_URL_204="x"' > "${src}/scripts/net-common.sh"
    printf '%s\n' '#!/bin/bash' 'echo not a lib' > "${src}/scripts/failover-watchdog.sh"

    changed=0
    run apply_update "$src"

    [ "$status" -eq 0 ]
    grep -q "shared constants" "${UPDATE_ROUTER_LIB_DIR}/net-common.sh"
    # Lib delivery must be 0644 (data, not executable)
    [ ! -x "${UPDATE_ROUTER_LIB_DIR}/net-common.sh" ]
    [ ! -e "${UPDATE_ROUTER_LIB_DIR}/failover-watchdog.sh" ]
    grep -q "updated lib script: net-common.sh" "${TEST_ROOT}/update.log"
}
