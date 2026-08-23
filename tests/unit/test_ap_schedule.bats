#!/usr/bin/env bats
# Unit tests for scripts/ap-schedule.sh
# AP quiet-hours helper: hostapd_cli ping gate, uap0 disable/enable,
# ntfy notification on disable. hostapd_cli/logger are PATH mocks;
# the notify path is sed-patched into the mock bin.

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
SCRIPT="${SCRIPT_DIR}/scripts/ap-schedule.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"

    export HOSTAPD_READY="1"

    mock_cmd_script logger 'echo "$*" >> "$MOCK_BIN/logger.calls"; exit 0'
    mock_cmd_script notify-router.sh 'echo "$*" >> "$MOCK_BIN/notify-router.sh.calls"; exit 0'

    # hostapd_cli: ping answers PONG unless HOSTAPD_READY=0; -i uap0
    # disable/enable calls are logged
    mock_cmd_script hostapd_cli 'echo "$*" >> "$MOCK_BIN/hostapd_cli.calls"
if [ "$1" = "-p" ] && [ "$3" = "ping" ]; then
    if [ "$HOSTAPD_READY" = "1" ]; then
        printf "PONG\n"
        exit 0
    fi
    exit 1
fi
exit 0'
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

_run_schedule() {
    local patched="${_STATE_DIR}/patched.sh"
    sed "s|/usr/local/bin/notify-router.sh|${MOCK_BIN}/notify-router.sh|g" "$SCRIPT" > "$patched"
    bash "$patched" "$@"
}

@test "ap-schedule: no arguments prints usage and exits 1" {
    run _run_schedule
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage:"* ]]
}

@test "ap-schedule: unknown action prints usage and exits 1" {
    run _run_schedule toggle
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage:"* ]]
}

@test "ap-schedule: disable turns off uap0 when hostapd responds" {
    run _run_schedule disable
    [ "$status" -eq 0 ]
    grep -q -- "-p /var/run/hostapd -i uap0 disable" "$MOCK_BIN/hostapd_cli.calls"
    grep -q "AP disabled" "$MOCK_BIN/logger.calls"
}

@test "ap-schedule: disable sends ntfy notification with schedule window" {
    printf 'AP_DISABLE_TIME="22:30"\nAP_ENABLE_TIME="06:15"\n' > "${_STATE_DIR}/cfg"
    # cfg is sourced from /etc/default/travel-router — patch that too
    local patched="${_STATE_DIR}/patched.sh"
    sed -e "s|/usr/local/bin/notify-router.sh|${MOCK_BIN}/notify-router.sh|g" \
        -e "s|source /etc/default/travel-router|source '${_STATE_DIR}/cfg'|g" \
        "$SCRIPT" > "$patched"
    run bash "$patched" disable
    [ "$status" -eq 0 ]
    grep -q "AP disabled for the night (22:30–06:15)" "${MOCK_BIN}/notify-router.sh.calls"
}

@test "ap-schedule: disable skips cleanly when hostapd socket is not ready" {
    export HOSTAPD_READY="0"
    run _run_schedule disable
    [ "$status" -eq 0 ]
    grep -qi "socket not ready" "$MOCK_BIN/logger.calls"
    [[ ! -f "${MOCK_BIN}/hostapd_cli.calls" ]] || ! grep -q "uap0 disable" "$MOCK_BIN/hostapd_cli.calls"
}

@test "ap-schedule: enable turns on uap0 when hostapd responds" {
    run _run_schedule enable
    [ "$status" -eq 0 ]
    grep -q -- "-p /var/run/hostapd -i uap0 enable" "$MOCK_BIN/hostapd_cli.calls"
    grep -q "AP enabled" "$MOCK_BIN/logger.calls"
}

@test "ap-schedule: enable fails loudly when hostapd never responds" {
    export HOSTAPD_READY="0"
    run _run_schedule enable
    [ "$status" -eq 1 ]
    ! grep -q -- "-i uap0 enable" "$(ls "$MOCK_BIN"/hostapd_cli.calls 2>/dev/null || echo /dev/null)"
}
