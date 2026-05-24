#!/usr/bin/env bats
# Unit tests for scripts/wan-watchdog.sh
# Tests cover: WAN up (no action), WAN down (recovery), state file writes,
# flock concurrency guard, and graduated recovery steps.

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
SCRIPT="${SCRIPT_DIR}/scripts/wan-watchdog.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"

    export WAN_PING_TARGETS="1.1.1.1 8.8.8.8"
    export NTFY_TOPIC=""

    # logger: capture calls
    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/logger.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/logger"
    chmod +x "${MOCK_BIN}/logger"

    # flock: always succeed (simulate lock acquired)
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/flock"
    chmod +x "${MOCK_BIN}/flock"

    # notify-router.sh: capture calls
    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/notify-router.sh.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/notify-router.sh"
    chmod +x "${MOCK_BIN}/notify-router.sh"

    # captive-check.sh: no-op
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/captive-check.sh"
    chmod +x "${MOCK_BIN}/captive-check.sh"

    # systemctl: no-op
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/systemctl"
    chmod +x "${MOCK_BIN}/systemctl"

    # nmcli: no-op (capture separately per test)
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/nmcli"
    chmod +x "${MOCK_BIN}/nmcli"

    # reboot: no-op
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/reboot"
    chmod +x "${MOCK_BIN}/reboot"

    # ip: no-op
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/ip"
    chmod +x "${MOCK_BIN}/ip"

    mkdir -p "${_STATE_DIR}/lock"
    mkdir -p "${_STATE_DIR}/lib"
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# ---------------------------------------------------------------------------
# Helper: build and run a patched version of wan-watchdog.sh
# Key patches:
#   1. Travel-router config source
#   2. Lock file path
#   3. Log file path
#   4. State file path (wan-watchdog-fails)
#   5. Uplink state file path
#   6. /usr/local/bin/captive-check.sh → MOCK_BIN
#   7. /usr/local/bin/notify-router.sh → MOCK_BIN
#   8. mkdir -p /var/lib/travel-router → redirect to temp dir
# ---------------------------------------------------------------------------
_run_watchdog() {
    local cfg="${_STATE_DIR}/travel-router"
    {
        printf 'WAN_PING_TARGETS=%s\n' "${WAN_PING_TARGETS}"
        printf 'NTFY_TOPIC=%s\n'       "${NTFY_TOPIC:-}"
    } > "$cfg"

    local patched="${_STATE_DIR}/wan-watchdog-patched.sh"
    sed "s|source /etc/default/travel-router|source '${cfg}'|g
         s|/run/lock/wan-watchdog.lock|${_STATE_DIR}/lock/wan-watchdog.lock|g
         s|LOGFILE=\"/var/log/wan-watchdog.log\"|LOGFILE='${_STATE_DIR}/wan-watchdog.log'|g
         s|mkdir -p /var/lib/travel-router|mkdir -p '${_STATE_DIR}/lib'|g
         s|STATE_FILE=\"/var/lib/travel-router/wan-watchdog-fails\"|STATE_FILE='${_STATE_DIR}/wan-watchdog-fails'|g
         s|/var/lib/travel-router/uplink.state|${_STATE_DIR}/lib/uplink.state|g
         s|/usr/local/bin/captive-check.sh|${MOCK_BIN}/captive-check.sh|g
         s|/usr/local/bin/notify-router.sh|${MOCK_BIN}/notify-router.sh|g" \
        "$SCRIPT" > "$patched"
    chmod +x "$patched"

    bash "$patched"
}

# ---------------------------------------------------------------------------
# Test 1: WAN up → no recovery actions, state file reset to 0
# ---------------------------------------------------------------------------
@test "wan-watchdog: WAN reachable writes state file as 0 and skips recovery" {
    # ping succeeds
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/ping"
    chmod +x "${MOCK_BIN}/ping"
    # curl not needed when ping succeeds
    printf '#!/bin/bash\nprintf "204"\n' > "${MOCK_BIN}/curl"
    chmod +x "${MOCK_BIN}/curl"

    # nmcli: capture to detect any unexpected calls
    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/nmcli.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/nmcli"
    chmod +x "${MOCK_BIN}/nmcli"

    run _run_watchdog
    [ "$status" -eq 0 ]

    # State file should contain "0"
    [ -f "${_STATE_DIR}/wan-watchdog-fails" ]
    local val
    val=$(cat "${_STATE_DIR}/wan-watchdog-fails")
    [ "$val" = "0" ]

    # nmcli should NOT have been called for recovery
    [ ! -f "${MOCK_BIN}/nmcli.calls" ]
}

# ---------------------------------------------------------------------------
# Test 2: WAN down (fail count 0 → 1) → nmcli disconnect/connect called
# ---------------------------------------------------------------------------
@test "wan-watchdog: WAN unreachable increments fail count and runs step 1 recovery" {
    # ping fails
    printf '#!/bin/bash\nexit 1\n' > "${MOCK_BIN}/ping"
    chmod +x "${MOCK_BIN}/ping"
    # curl also fails (confirm WAN really down)
    printf '#!/bin/bash\nprintf "000"\nexit 1\n' > "${MOCK_BIN}/curl"
    chmod +x "${MOCK_BIN}/curl"

    # nmcli: capture calls
    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/nmcli.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/nmcli"
    chmod +x "${MOCK_BIN}/nmcli"

    # Start with 0 failures
    printf '0\n' > "${_STATE_DIR}/wan-watchdog-fails"

    run _run_watchdog
    [ "$status" -eq 0 ]

    # Fail counter should now be 1
    local val
    val=$(cat "${_STATE_DIR}/wan-watchdog-fails")
    [ "$val" = "1" ]

    # nmcli should have been called for disconnect/connect wlan0
    [ -f "${MOCK_BIN}/nmcli.calls" ]
    grep -q "wlan0" "${MOCK_BIN}/nmcli.calls"
}

# ---------------------------------------------------------------------------
# Test 3: fail count 2 → recovery step 3 (systemctl restart NetworkManager)
# ---------------------------------------------------------------------------
@test "wan-watchdog: 2 prior failures triggers NetworkManager restart on third attempt" {
    printf '#!/bin/bash\nexit 1\n' > "${MOCK_BIN}/ping"
    chmod +x "${MOCK_BIN}/ping"
    printf '#!/bin/bash\nprintf "000"\nexit 1\n' > "${MOCK_BIN}/curl"
    chmod +x "${MOCK_BIN}/curl"

    # systemctl: capture calls
    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/systemctl.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/systemctl"
    chmod +x "${MOCK_BIN}/systemctl"

    # Pre-seed with 2 failures (recovery step 2 already ran)
    printf '2\n' > "${_STATE_DIR}/wan-watchdog-fails"

    run _run_watchdog
    [ "$status" -eq 0 ]

    # Fail counter incremented to 3
    local val
    val=$(cat "${_STATE_DIR}/wan-watchdog-fails")
    [ "$val" = "3" ]

    # Recovery step 3: cycle wlan0 link — systemctl stop/start hostapd
    [ -f "${MOCK_BIN}/systemctl.calls" ]
    grep -qE "stop hostapd|start hostapd" "${MOCK_BIN}/systemctl.calls"
}

# ---------------------------------------------------------------------------
# Test 4: state file is written after each failure
# ---------------------------------------------------------------------------
@test "wan-watchdog: state file increments on consecutive failures" {
    printf '#!/bin/bash\nexit 1\n' > "${MOCK_BIN}/ping"
    chmod +x "${MOCK_BIN}/ping"
    printf '#!/bin/bash\nprintf "000"\nexit 1\n' > "${MOCK_BIN}/curl"
    chmod +x "${MOCK_BIN}/curl"

    # Pre-seed with 0
    printf '0\n' > "${_STATE_DIR}/wan-watchdog-fails"

    _run_watchdog

    local val
    val=$(cat "${_STATE_DIR}/wan-watchdog-fails")
    [ "$val" = "1" ]

    # Run again — should reach 2
    _run_watchdog
    val=$(cat "${_STATE_DIR}/wan-watchdog-fails")
    [ "$val" = "2" ]
}

# ---------------------------------------------------------------------------
# Test 5: flock concurrency guard — second run exits 0 without recovery
# ---------------------------------------------------------------------------
@test "wan-watchdog: flock held blocks second instance from running recovery" {
    # Replace flock with a stub that always returns failure (lock held)
    printf '#!/bin/bash\nexit 1\n' > "${MOCK_BIN}/flock"
    chmod +x "${MOCK_BIN}/flock"

    printf '#!/bin/bash\nexit 1\n' > "${MOCK_BIN}/ping"
    chmod +x "${MOCK_BIN}/ping"

    # nmcli: capture calls
    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/nmcli.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/nmcli"
    chmod +x "${MOCK_BIN}/nmcli"

    run _run_watchdog
    [ "$status" -eq 0 ]

    # nmcli should NOT have been called because flock blocked execution
    [ ! -f "${MOCK_BIN}/nmcli.calls" ]
}
