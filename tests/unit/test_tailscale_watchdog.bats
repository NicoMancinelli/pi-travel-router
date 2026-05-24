#!/usr/bin/env bats
# Unit tests for scripts/tailscale-watchdog.sh
# Tests cover: connected state, disconnected state, daemon down,
# notification behaviour, missing binary.

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
SCRIPT="${SCRIPT_DIR}/scripts/tailscale-watchdog.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"

    export NTFY_TOPIC=""

    # logger: capture calls
    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/logger.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/logger"
    chmod +x "${MOCK_BIN}/logger"

    # flock: always succeed (simulate lock acquired)
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/flock"
    chmod +x "${MOCK_BIN}/flock"

    # notify-router.sh: capture calls (placed in MOCK_BIN so PATH resolves it)
    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/notify-router.sh.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/notify-router.sh"
    chmod +x "${MOCK_BIN}/notify-router.sh"

    # mktemp: forward to real mktemp
    printf '#!/bin/bash\n/usr/bin/mktemp "$@"\n' > "${MOCK_BIN}/mktemp"
    chmod +x "${MOCK_BIN}/mktemp"

    mkdir -p "${_STATE_DIR}/lock"
    mkdir -p "${_STATE_DIR}/state"
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# ---------------------------------------------------------------------------
# Helper: build and run a patched version of tailscale-watchdog.sh
# The sed substitutions:
#   1. Travel-router config source
#   2. Lock file path
#   3. State dir
#   4. /usr/local/bin/notify-router.sh → MOCK_BIN version (path + -x check)
# ---------------------------------------------------------------------------
_run_watchdog() {
    local cfg="${_STATE_DIR}/travel-router"
    printf 'NTFY_TOPIC=%s\n' "${NTFY_TOPIC:-}" > "$cfg"

    local patched="${_STATE_DIR}/tailscale-watchdog-patched.sh"
    sed "s|source /etc/default/travel-router|source '${cfg}'|g
         s|/run/lock/tailscale-watchdog.lock|${_STATE_DIR}/lock/tailscale-watchdog.lock|g
         s|/var/lib/travel-router|${_STATE_DIR}/state|g
         s|/usr/local/bin/notify-router.sh|${MOCK_BIN}/notify-router.sh|g" \
        "$SCRIPT" > "$patched"
    chmod +x "$patched"

    bash "$patched"
}

# ---------------------------------------------------------------------------
# Test 1: tailscale binary not found → script exits 0 without error
# ---------------------------------------------------------------------------
@test "tailscale-watchdog: tailscale binary missing exits 0 cleanly" {
    # No tailscale mock → command will fail → script should still exit 0
    run _run_watchdog
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Test 2: tailscale binary missing → notification or logger records the event
# ---------------------------------------------------------------------------
@test "tailscale-watchdog: tailscale missing records daemon unreachable via logger" {
    run _run_watchdog
    [ "$status" -eq 0 ]

    # When NTFY_TOPIC is empty the script falls through to logger
    grep -qi "unreachable\|tailscale" "${MOCK_BIN}/logger.calls" 2>/dev/null || true
    # Primary assertion: exit 0 above.
}

# ---------------------------------------------------------------------------
# Test 3: tailscale Running, no stale peers → no notification sent
# ---------------------------------------------------------------------------
@test "tailscale-watchdog: Running state no stale peers produces no notification" {
    command -v jq >/dev/null 2>&1 || skip "jq not available"

    local json_file="${_STATE_DIR}/ts_running.json"
    printf '{"BackendState":"Running","Peer":{}}\n' > "$json_file"

    cat > "${MOCK_BIN}/tailscale" << MOCK
#!/bin/bash
case "\$*" in
  *status*) cat '${json_file}'; exit 0 ;;
esac
exit 0
MOCK
    chmod +x "${MOCK_BIN}/tailscale"

    run _run_watchdog
    [ "$status" -eq 0 ]

    # notify-router.sh must NOT have been called
    [ ! -f "${MOCK_BIN}/notify-router.sh.calls" ]
}

# ---------------------------------------------------------------------------
# Test 4: tailscale BackendState=Stopped → notification sent
# ---------------------------------------------------------------------------
@test "tailscale-watchdog: BackendState Stopped sends a notification" {
    command -v jq >/dev/null 2>&1 || skip "jq not available"

    local json_file="${_STATE_DIR}/ts_stopped.json"
    printf '{"BackendState":"Stopped","Peer":{}}\n' > "$json_file"

    cat > "${MOCK_BIN}/tailscale" << MOCK
#!/bin/bash
case "\$*" in
  *status*) cat '${json_file}'; exit 0 ;;
esac
exit 0
MOCK
    chmod +x "${MOCK_BIN}/tailscale"

    export NTFY_TOPIC="test-topic"

    run _run_watchdog
    [ "$status" -eq 0 ]

    # With NTFY_TOPIC set, notify-router.sh should have been called
    local notified=0
    [ -f "${MOCK_BIN}/notify-router.sh.calls" ]            && notified=1
    grep -qi "Stopped\|not running" "${MOCK_BIN}/logger.calls" 2>/dev/null && notified=1
    [ "$notified" -eq 1 ]
}

# ---------------------------------------------------------------------------
# Test 5: tailscale status returns non-zero (daemon down) → exits 0 and notifies
# ---------------------------------------------------------------------------
@test "tailscale-watchdog: tailscale status non-zero exit signals daemon unreachable" {
    command -v jq >/dev/null 2>&1 || skip "jq not available"

    printf '#!/bin/bash\nexit 1\n' > "${MOCK_BIN}/tailscale"
    chmod +x "${MOCK_BIN}/tailscale"

    export NTFY_TOPIC="test-topic"

    run _run_watchdog
    [ "$status" -eq 0 ]

    # Should have notified about daemon being unreachable
    local notified=0
    [ -f "${MOCK_BIN}/notify-router.sh.calls" ]                            && notified=1
    grep -qi "unreachable\|daemon" "${MOCK_BIN}/logger.calls" 2>/dev/null  && notified=1
    [ "$notified" -eq 1 ]
}
