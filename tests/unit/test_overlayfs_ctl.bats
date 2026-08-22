#!/usr/bin/env bats
# Unit tests for scripts/overlayfs-ctl.sh
# Tests overlay detection from the kernel cmdline and raspi-config invocation.

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
OVERLAY_SCRIPT="${SCRIPT_DIR}/scripts/overlayfs-ctl.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"
    export OVERLAYFS_CMDLINE="${_STATE_DIR}/cmdline"

    # Default: overlay not active
    echo "console=serial0,115200 console=tty1 root=PARTUUID=xxx rootfstype=ext4 fsck.repair=yes rootwait" \
        > "${OVERLAYFS_CMDLINE}"

    mock_cmd_capture "raspi-config" "" 0
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

@test "overlayfs-ctl: no arguments prints usage and exits 1" {
    run bash "${OVERLAY_SCRIPT}"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage:"* ]]
}

@test "overlayfs-ctl: status reports inactive on normal cmdline" {
    run bash "${OVERLAY_SCRIPT}" status
    [ "$status" -eq 0 ]
    [[ "$output" == *'"active": false'* ]]
}

@test "overlayfs-ctl: status reports active when boot=overlay present" {
    echo "console=tty1 boot=overlay root=PARTUUID=xxx rootwait" > "${OVERLAYFS_CMDLINE}"
    run bash "${OVERLAY_SCRIPT}" status
    [ "$status" -eq 0 ]
    [[ "$output" == *'"active": true'* ]]
}

@test "overlayfs-ctl: enable calls raspi-config nonint do_overlayfs 0" {
    run bash "${OVERLAY_SCRIPT}" enable
    [ "$status" -eq 0 ]
    [[ "$(mock_calls raspi-config)" == *"nonint do_overlayfs 0"* ]]
    [[ "$output" == *"reboot"* ]]
}

@test "overlayfs-ctl: enable when already active is a no-op" {
    echo "console=tty1 boot=overlay root=PARTUUID=xxx rootwait" > "${OVERLAYFS_CMDLINE}"
    run bash "${OVERLAY_SCRIPT}" enable
    [ "$status" -eq 0 ]
    [[ "$output" == *"already active"* ]]
    [ -z "$(mock_calls raspi-config)" ]
}

@test "overlayfs-ctl: disable calls raspi-config nonint do_overlayfs 1" {
    run bash "${OVERLAY_SCRIPT}" disable
    [ "$status" -eq 0 ]
    [[ "$(mock_calls raspi-config)" == *"nonint do_overlayfs 1"* ]]
}

@test "overlayfs-ctl: disable while overlay active mentions reboot" {
    echo "console=tty1 boot=overlay root=PARTUUID=xxx rootwait" > "${OVERLAYFS_CMDLINE}"
    run bash "${OVERLAY_SCRIPT}" disable
    [ "$status" -eq 0 ]
    [[ "$output" == *"reboot"* ]]
}

@test "overlayfs-ctl: enable propagates raspi-config failure" {
    mock_cmd "raspi-config" "overlay error" 1
    run bash "${OVERLAY_SCRIPT}" enable
    [ "$status" -ne 0 ]
}
