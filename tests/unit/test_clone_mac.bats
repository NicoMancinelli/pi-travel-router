#!/usr/bin/env bats
# Unit tests for scripts/clone-mac.sh
# MAC cloning for captive-portal auth: validation, hostapd stop/start
# lifecycle, macchanger -m/-p selection, persistence file, notifications.
# The root check, log path and state paths are sed-patched; macchanger,
# ip, systemctl and notify are PATH mocks.

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
SCRIPT="${SCRIPT_DIR}/scripts/clone-mac.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"
    export CFG="${_STATE_DIR}/travel-router"
    : > "$CFG"

    export HOSTAPD_ACTIVE="1"
    export CURRENT_MAC="aa:bb:cc:dd:ee:ff"

    mock_cmd_script logger 'echo "$*" >> "$MOCK_BIN/logger.calls"; exit 0'

    mock_cmd_script date 'echo "2026-08-23 00:00:00"; exit 0'
    mock_cmd_script notify-router.sh 'echo "$*" >> "$MOCK_BIN/notify-router.sh.calls"; exit 0'

    # ip: link show reports the current ether address; set/other calls logged
    mock_cmd_script ip 'echo "$*" >> "$MOCK_BIN/ip.calls"
if [ "$1" = "link" ] && [ "$2" = "show" ]; then
    printf "6: wlan0: <BROADCAST,MULTICAST,UP> mtu 1500\n"
    printf "    link/ether %s brd ff:ff:ff:ff:ff:ff\n" "$CURRENT_MAC"
    exit 0
fi
exit 0'

    # macchanger: log invocation (the -m vs -p distinction is what we assert)
    mock_cmd_script macchanger 'echo "$*" >> "$MOCK_BIN/macchanger.calls"; exit 0'

    # systemctl: is-active hostapd reflects HOSTAPD_ACTIVE; rest logged
    mock_cmd_script systemctl 'echo "$*" >> "$MOCK_BIN/systemctl.calls"
if [ "$1" = "is-active" ]; then
    [ "$HOSTAPD_ACTIVE" = "1" ] && exit 0 || exit 3
fi
exit 0'
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# Build a patched copy: drop the EUID gate (tests run unprivileged), redirect
# the sourced defaults file, log path and persistence dir into the temp dir,
# and point notify at a capture mock.
_build_patched() {
    local patched="${_STATE_DIR}/patched.sh"
    sed -e 's|^\[\[ \$EUID -ne 0 \]\] && die.*||' \
        -e "s|source /etc/default/travel-router|source '${CFG}'|g" \
        -e "s|^LOG=.*|LOG=\"${_STATE_DIR}/clone-mac.log\"|" \
        -e "s|/var/lib/travel-router|${_STATE_DIR}/state|g" \
        -e "s|/usr/local/bin/notify-router.sh|${MOCK_BIN}/notify-router.sh|g" \
        "$SCRIPT" > "$patched"
    echo "$patched"
}

@test "clone-mac: no arguments prints usage and exits 1" {
    run bash "$(_build_patched)"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage:"* ]]
}

@test "clone-mac: invalid MAC format is rejected" {
    run bash "$(_build_patched)" "not-a-mac"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Invalid MAC address"* ]]
}

@test "clone-mac: non-hex octets are rejected" {
    run bash "$(_build_patched)" "ZZ:BB:CC:DD:EE:FF"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Invalid MAC address"* ]]
}

@test "clone-mac: valid clone brings wlan0 down, runs macchanger -m, brings it up" {
    run bash "$(_build_patched)" "11:22:33:44:55:66"
    [ "$status" -eq 0 ]

    local ipcalls maccalls
    ipcalls="$(mock_calls ip)"
    maccalls="$(mock_calls macchanger)"

    grep -q "link set wlan0 down" <<< "$ipcalls"
    grep -q -- "-m 11:22:33:44:55:66 wlan0" <<< "$maccalls"
    grep -q "link set wlan0 up" <<< "$ipcalls"
    [[ "$output" == *"wlan0 MAC is now 11:22:33:44:55:66"* ]]
}

@test "clone-mac: stops hostapd before downlink and restarts it after" {
    run bash "$(_build_patched)" "11:22:33:44:55:66"
    [ "$status" -eq 0 ]
    local calls
    calls="$(mock_calls systemctl)"
    grep -q "stop hostapd" <<< "$calls"
    grep -q "start hostapd" <<< "$calls"
}

@test "clone-mac: leaves hostapd alone when it was not running" {
    export HOSTAPD_ACTIVE="0"
    run bash "$(_build_patched)" "11:22:33:44:55:66"
    [ "$status" -eq 0 ]
    local calls
    calls="$(mock_calls systemctl)"
    ! grep -q "stop hostapd" <<< "$calls"
    ! grep -q "^start hostapd" <<< "$calls"
}

@test "clone-mac: persists cloned MAC to state file" {
    run bash "$(_build_patched)" "aa:22:33:44:55:66"
    [ "$status" -eq 0 ]
    grep -q "aa:22:33:44:55:66" "${_STATE_DIR}/state/cloned-mac"
}

@test "clone-mac: notifies with the target MAC" {
    run bash "$(_build_patched)" "aa:22:33:44:55:66"
    [ "$status" -eq 0 ]
    grep -q "MAC cloned to aa:22:33:44:55:66" "${MOCK_BIN}/notify-router.sh.calls"
}

@test "clone-mac: short-circuits when wlan0 already has the target MAC" {
    run bash "$(_build_patched)" "AA:BB:CC:DD:EE:FF"
    [ "$status" -eq 0 ]
    [[ "$output" == *"nothing to do"* ]]
    [ ! -f "$MOCK_BIN/macchanger.calls" ]
}

@test "clone-mac: --restore uses macchanger -p (permanent, not random)" {
    run bash "$(_build_patched)" --restore
    [ "$status" -eq 0 ]
    grep -q -- "-p wlan0" "$MOCK_BIN/macchanger.calls"
    ! grep -q -- "-r wlan0" "$MOCK_BIN/macchanger.calls"
    grep -q "MAC restored" "${_STATE_DIR}/clone-mac.log"
}

@test "clone-mac: --show prints the current wlan0 MAC without changes" {
    run bash "$(_build_patched)" --show
    [ "$status" -eq 0 ]
    [[ "$output" == *"wlan0 current MAC: aa:bb:cc:dd:ee:ff"* ]]
    [ ! -f "$MOCK_BIN/macchanger.calls" ]
}
