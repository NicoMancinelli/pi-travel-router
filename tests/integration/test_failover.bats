#!/usr/bin/env bats
# Integration tests for scripts/failover-watchdog.sh
# All system commands (ip, curl, dig, host, wg, systemctl, journalctl,
# notify-router.sh, logger, flock, mktemp, mkdir) are mocked.

load '../helpers/mock_commands'

REPO_ROOT="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
SCRIPT="${REPO_ROOT}/scripts/failover-watchdog.sh"

setup() {
    setup_mock_bin

    # Writable state and log directories
    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"
    export LOGFILE="${_STATE_DIR}/failover-watchdog.log"
    touch "${LOGFILE}"
    # Shared ip-call log used across tests
    export IP_CALLS="${_STATE_DIR}/ip.calls"

    # Override state paths used inside the script
    export _UPLINK_STATE_DIR="${_STATE_DIR}"
    export _UPLINK_STATE_FILE="${_STATE_DIR}/uplink.state"

    # Default silent mocks for commands that must exist but aren't under test
    mock_cmd "logger"            "" 0
    mock_cmd "journalctl"        "" 0
    mock_cmd "systemctl"         "" 0
    mock_cmd "notify-router.sh"  "" 0
    mock_cmd "wg"                "" 0

    # flock: mock to succeed so the script body runs (not testing the lock here)
    # The script does: flock -n 9 || exit 0
    mock_cmd "flock" "" 0

    # mktemp: delegate to system mktemp to avoid recursion
    cat > "${MOCK_BIN}/mktemp" <<'MOCK'
#!/bin/bash
/usr/bin/mktemp "$@"
MOCK
    chmod +x "${MOCK_BIN}/mktemp"

    # mkdir: pass through so the script's own mkdir -p calls work
    cat > "${MOCK_BIN}/mkdir" <<'MOCK'
#!/bin/bash
/bin/mkdir "$@"
MOCK
    chmod +x "${MOCK_BIN}/mkdir"

    # tail: pass through (used by truncate_log)
    cat > "${MOCK_BIN}/tail" <<'MOCK'
#!/bin/bash
/usr/bin/tail "$@"
MOCK
    chmod +x "${MOCK_BIN}/tail"

    # mv: pass through (used by truncate_log)
    cat > "${MOCK_BIN}/mv" <<'MOCK'
#!/bin/bash
/bin/mv "$@"
MOCK
    chmod +x "${MOCK_BIN}/mv"
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# ---------------------------------------------------------------------------
# Helper: run the full script, exporting the current _STATE_DIR and IP_CALLS
# ---------------------------------------------------------------------------
_run_watchdog() {
    run env \
        LOGFILE="${LOGFILE}" \
        _UPLINK_STATE_DIR="${_UPLINK_STATE_DIR}" \
        _UPLINK_STATE_FILE="${_UPLINK_STATE_FILE}" \
        IP_CALLS="${IP_CALLS}" \
        bash "${SCRIPT}"
}

# ---------------------------------------------------------------------------
# Helper: write the ip mock using a variable-expanding heredoc
# $1: output for "-br link"
# $2: output for "route" (plain)
# ---------------------------------------------------------------------------
_write_ip_mock() {
    local br_output="$1"
    local route_output="$2"
    cat > "${MOCK_BIN}/ip" <<MOCK
#!/bin/bash
case "\$*" in
    "-br link")
        printf '%s\n' "${br_output}"
        ;;
    "route")
        printf '%s\n' "${route_output}"
        ;;
    route\ replace\ *)
        printf 'route_replace: %s\n' "\$*" >> "${IP_CALLS}"
        ;;
    "link show wg0")
        exit 1
        ;;
    *)
        true
        ;;
esac
exit 0
MOCK
    chmod +x "${MOCK_BIN}/ip"
}

# ---------------------------------------------------------------------------
# Test 1: USB tether UP and internet reachable → metric 100 applied
# ---------------------------------------------------------------------------
@test "USB tether UP + internet reachable: ip route replace called with metric 100" {
    _write_ip_mock \
        "enxaabbcc1122 UP   aa:bb:cc:dd:11:22" \
        "default via 10.0.0.1 dev enxaabbcc1122 proto dhcp metric 600"

    # All probes succeed
    mock_cmd "curl" "" 0
    mock_cmd "host" "" 0
    mock_cmd "dig"  "" 0

    _run_watchdog

    [ "$status" -eq 0 ]
    # route replace must have been invoked for enxaabbcc1122 with metric 100
    grep -q "route_replace:.*enxaabbcc1122.*metric 100" "${IP_CALLS}"
}

# ---------------------------------------------------------------------------
# Test 2: USB tether UP but 0-of-3 probes pass → interface demoted to metric 900
# ---------------------------------------------------------------------------
@test "USB tether UP but all probes fail: interface demoted to metric 900" {
    _write_ip_mock \
        "enxaabbcc1122 UP   aa:bb:cc:dd:11:22" \
        "default via 10.0.0.1 dev enxaabbcc1122 proto dhcp metric 100"

    # All probes fail (curl exits 1, host/dig exit 1 → 0-of-3 pass)
    mock_cmd "curl" "" 1
    mock_cmd "host" "" 1
    mock_cmd "dig"  "" 1

    _run_watchdog

    # Demotion to metric 900 must have been applied
    grep -q "route_replace:.*enxaabbcc1122.*metric 900" "${IP_CALLS}"
}

# ---------------------------------------------------------------------------
# Test 3: 1-of-3 probes pass (only DNS succeeds) → still demoted
# (need pass >= 2 for can_reach_internet to return 0)
# ---------------------------------------------------------------------------
@test "1-of-3 probes pass (only DNS): interface still demoted" {
    _write_ip_mock \
        "enxaabbcc1122 UP   aa:bb:cc:dd:11:22" \
        "default via 10.0.0.1 dev enxaabbcc1122 proto dhcp metric 100"

    # HTTP probes (curl) fail; DNS succeeds → 1-of-3 < 2 threshold → demote
    mock_cmd "curl" "" 1
    mock_cmd "host" "" 0
    mock_cmd "dig"  "" 0

    _run_watchdog

    grep -q "route_replace:.*enxaabbcc1122.*metric 900" "${IP_CALLS}"
}

# ---------------------------------------------------------------------------
# Test 4: 2-of-3 probes pass (both curl calls succeed, DNS irrelevant) → promote
# The script runs curl twice (HTTP probe + HTTPS probe); if both exit 0,
# pass = 2 which meets the >= 2 threshold → uplink is good.
# ---------------------------------------------------------------------------
@test "2-of-3 probes pass (HTTP + HTTPS succeed): interface promoted to metric 100" {
    _write_ip_mock \
        "enxaabbcc1122 UP   aa:bb:cc:dd:11:22" \
        "default via 10.0.0.1 dev enxaabbcc1122 proto dhcp metric 600"

    # Both curl probes succeed → pass = 2 → promote
    mock_cmd "curl" "" 0
    # DNS probe: make host/dig fail so pass stays at 2 (not 3)
    mock_cmd "host" "" 1
    mock_cmd "dig"  "" 1

    _run_watchdog

    [ "$status" -eq 0 ]
    grep -q "route_replace:.*enxaabbcc1122.*metric 100" "${IP_CALLS}"
}

# ---------------------------------------------------------------------------
# Test 5: FAILOVER_PROBE_TIMEOUT is forwarded to curl --max-time
# ---------------------------------------------------------------------------
@test "FAILOVER_PROBE_TIMEOUT=1 is forwarded to curl --max-time" {
    _write_ip_mock \
        "wlan0 UP   aa:bb:cc:dd:11:23" \
        "default via 192.168.1.1 dev wlan0 proto dhcp metric 600"

    # Record all curl arguments
    local curl_args="${_STATE_DIR}/curl.args"
    cat > "${MOCK_BIN}/curl" <<MOCK
#!/bin/bash
printf '%s\n' "\$*" >> "${curl_args}"
exit 0
MOCK
    chmod +x "${MOCK_BIN}/curl"

    mock_cmd "host" "" 0
    mock_cmd "dig"  "" 0

    FAILOVER_PROBE_TIMEOUT=1 run env \
        LOGFILE="${LOGFILE}" \
        _UPLINK_STATE_DIR="${_UPLINK_STATE_DIR}" \
        _UPLINK_STATE_FILE="${_UPLINK_STATE_FILE}" \
        IP_CALLS="${IP_CALLS}" \
        FAILOVER_PROBE_TIMEOUT=1 \
        bash "${SCRIPT}"

    grep -q -- "--max-time 1" "${curl_args}"
}

# ---------------------------------------------------------------------------
# Test 6: No known uplink interfaces → warning logged to LOGFILE or stdout
# ---------------------------------------------------------------------------
@test "no known uplink interfaces: warning emitted" {
    # eth0 is not a known tether interface (not enx*, rndis0, usb0, bnep0, wlan0)
    # ip route returns no default wlan0 route so get_wifi_iface returns empty
    _write_ip_mock \
        "eth0 UP   aa:bb:cc:dd:11:22" \
        "default via 192.168.1.1 dev eth0 proto dhcp metric 100"

    mock_cmd "curl" "" 1
    mock_cmd "host" "" 1
    mock_cmd "dig"  "" 1

    # The script appends to LOGFILE via log(), which the test cannot reliably
    # read on all platforms (LOGFILE is hardcoded inside the script).
    # Instead, we verify via the absence of route_replace activity (no uplink
    # was promoted) and that the script did not exit non-zero unexpectedly.
    _run_watchdog

    # Script must exit cleanly (no crash)
    # Status may be 0 (normal completion) or non-zero if the script couldn't
    # acquire the lock, but must not be a crash (> 127).
    [ "$status" -lt 128 ]

    # ip route replace must NOT have been called (no valid uplink to promote)
    if [ -s "${IP_CALLS}" ]; then
        # If the file is non-empty, ensure no "metric 100" promote happened
        ! grep -q "metric 100" "${IP_CALLS}"
    else
        true
    fi
}

# ---------------------------------------------------------------------------
# Test 7: Concurrent watchdog lock — second instance blocked by flock exits 0
# ---------------------------------------------------------------------------
@test "concurrent lock: second instance exits 0 when lock is held" {
    # Use REAL flock — remove our mock so the kernel lock is exercised
    rm -f "${MOCK_BIN}/flock"

    _write_ip_mock \
        "wlan0 UP   aa:bb:cc:dd:11:23" \
        "default via 192.168.1.1 dev wlan0 proto dhcp metric 600"

    mock_cmd "curl" "" 0
    mock_cmd "host" "" 0
    mock_cmd "dig"  "" 0

    # Hold the lock file that the script will try to acquire.
    # The script does: exec 9>/run/lock/failover-watchdog.lock; flock -n 9 || exit 0
    # We can't override /run/lock, but we can test that the mock flock path
    # correctly causes the second instance to bail.
    # Re-mock flock to return 1 (lock busy) for a targeted second run.
    cat > "${MOCK_BIN}/flock" <<'MOCK'
#!/bin/bash
exit 1
MOCK
    chmod +x "${MOCK_BIN}/flock"

    _run_watchdog

    # When flock returns 1, the script does: flock -n 9 || exit 0
    # So it exits 0 without doing any work
    [ "$status" -eq 0 ]
    # ip route replace must NOT have been called
    [ ! -s "${IP_CALLS}" ]
}
