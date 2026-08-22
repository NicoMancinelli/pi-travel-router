#!/usr/bin/env bats
# Unit tests for scripts/usb-share.sh
# Tests smb.conf generation, share name validation, and smbd lifecycle calls.

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
SHARE_SCRIPT="${SCRIPT_DIR}/scripts/usb-share.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"

    # Redirect all file paths into the temp dir
    export USB_SHARE_DEFAULTS="${_STATE_DIR}/travel-router-defaults"
    export USB_SHARE_SMB_CONF="${_STATE_DIR}/smb.conf"
    export USB_SHARE_PATH="${_STATE_DIR}/media"
    : > "${USB_SHARE_DEFAULTS}"

    # Default mocks: samba present, share mounted, systemd happy
    mock_cmd "smbd"        "" 0
    mock_cmd "testparm"    "" 0
    mock_cmd "mountpoint"  "" 0
    mock_cmd_capture "systemctl" "" 0
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

@test "usb-share: no arguments prints usage and exits 1" {
    run bash "${SHARE_SCRIPT}"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage:"* ]]
}

@test "usb-share: unknown subcommand exits 1" {
    run bash "${SHARE_SCRIPT}" bogus
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage:"* ]]
}

@test "usb-share: enable writes smb.conf with default share name and path" {
    run bash "${SHARE_SCRIPT}" enable
    [ "$status" -eq 0 ]
    grep -q '^\[TravelData\]' "${USB_SHARE_SMB_CONF}"
    grep -q "path = ${USB_SHARE_PATH}" "${USB_SHARE_SMB_CONF}"
}

@test "usb-share: enable defaults to a writable share" {
    run bash "${SHARE_SCRIPT}" enable
    [ "$status" -eq 0 ]
    grep -q 'read only = no' "${USB_SHARE_SMB_CONF}"
}

@test "usb-share: USB_SHARE_RO=1 produces a read-only share" {
    export USB_SHARE_RO=1
    run bash "${SHARE_SCRIPT}" enable
    [ "$status" -eq 0 ]
    grep -q 'read only = yes' "${USB_SHARE_SMB_CONF}"
}

@test "usb-share: custom USB_SHARE_NAME is used as section name" {
    export USB_SHARE_NAME="RoadFiles"
    run bash "${SHARE_SCRIPT}" enable
    [ "$status" -eq 0 ]
    grep -q '^\[RoadFiles\]' "${USB_SHARE_SMB_CONF}"
    [[ "$output" == *"RoadFiles"* ]]
}

@test "usb-share: invalid USB_SHARE_NAME is rejected" {
    export USB_SHARE_NAME="bad name; rm -rf /"
    run bash "${SHARE_SCRIPT}" enable
    [ "$status" -eq 1 ]
    [[ "$output" == *"invalid USB_SHARE_NAME"* ]]
}

@test "usb-share: enable starts and enables smbd, keeps nmbd off" {
    run bash "${SHARE_SCRIPT}" enable
    [ "$status" -eq 0 ]
    calls="$(mock_calls systemctl)"
    [[ "$calls" == *"enable --now smbd"* ]]
    [[ "$calls" == *"disable --now nmbd"* ]]
}

@test "usb-share: enable backs up an existing distro smb.conf" {
    echo "; original distro config" > "${USB_SHARE_SMB_CONF}"
    run bash "${SHARE_SCRIPT}" enable
    [ "$status" -eq 0 ]
    [ -f "${USB_SHARE_SMB_CONF}.orig" ]
    grep -q 'original distro config' "${USB_SHARE_SMB_CONF}.orig"
}

@test "usb-share: re-enable does not overwrite the .orig backup" {
    echo "; original distro config" > "${USB_SHARE_SMB_CONF}"
    bash "${SHARE_SCRIPT}" enable
    bash "${SHARE_SCRIPT}" enable
    grep -q 'original distro config' "${USB_SHARE_SMB_CONF}.orig"
}

@test "usb-share: enable warns when mount point is not mounted" {
    mock_cmd "mountpoint" "" 1
    run bash "${SHARE_SCRIPT}" enable
    [ "$status" -eq 0 ]
    [[ "$output" == *"not a mountpoint"* ]]
}

@test "usb-share: enable fails when testparm rejects the config" {
    mock_cmd "testparm" "" 1
    run bash "${SHARE_SCRIPT}" enable
    [ "$status" -eq 1 ]
    [[ "$output" == *"testparm"* ]]
}

@test "usb-share: disable stops and disables smbd" {
    run bash "${SHARE_SCRIPT}" disable
    [ "$status" -eq 0 ]
    calls="$(mock_calls systemctl)"
    [[ "$calls" == *"disable --now smbd"* ]]
}

@test "usb-share: status reports enabled=true when smbd is active" {
    mock_cmd_script "systemctl" 'exit 0'
    run bash "${SHARE_SCRIPT}" status
    [ "$status" -eq 0 ]
    [[ "$output" == *'"enabled": true'* ]]
    [[ "$output" == *'"mounted": true'* ]]
}

@test "usb-share: status reports enabled=false when smbd is inactive" {
    mock_cmd_script "systemctl" 'exit 3'
    mock_cmd "mountpoint" "" 1
    run bash "${SHARE_SCRIPT}" status
    [ "$status" -eq 0 ]
    [[ "$output" == *'"enabled": false'* ]]
    [[ "$output" == *'"mounted": false'* ]]
}

@test "usb-share: status reflects USB_SHARE_RO setting" {
    export USB_SHARE_RO=1
    run bash "${SHARE_SCRIPT}" status
    [ "$status" -eq 0 ]
    [[ "$output" == *'"read_only": true'* ]]
}

@test "usb-share: defaults file values are honoured" {
    cat > "${USB_SHARE_DEFAULTS}" << 'EOF'
USB_SHARE_NAME="HolidayPics"
USB_SHARE_RO="1"
EOF
    run bash "${SHARE_SCRIPT}" enable
    [ "$status" -eq 0 ]
    grep -q '^\[HolidayPics\]' "${USB_SHARE_SMB_CONF}"
    grep -q 'read only = yes' "${USB_SHARE_SMB_CONF}"
}
