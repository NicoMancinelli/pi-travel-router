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
