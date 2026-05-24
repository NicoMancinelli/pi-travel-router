#!/usr/bin/env bats
# Unit tests for scripts/wireguard-watchdog.sh
# Tests cover: enable flag, interface down/up detection, stale handshake logic,
# and notification behaviour.

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
SCRIPT="${SCRIPT_DIR}/scripts/wireguard-watchdog.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"

    # Default config: WireGuard enabled, interface wg0
    export ENABLE_WIREGUARD="1"
    export WG_INTERFACE="wg0"
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

    # systemctl: no-op by default
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/systemctl"
    chmod +x "${MOCK_BIN}/systemctl"

    # wg: no-op by default
    printf '#!/bin/bash\nexit 0\n' > "${MOCK_BIN}/wg"
    chmod +x "${MOCK_BIN}/wg"

    mkdir -p "${_STATE_DIR}/lock"
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# ---------------------------------------------------------------------------
# Helper: build and run a patched version of wireguard-watchdog.sh
# ---------------------------------------------------------------------------
_run_watchdog() {
    local cfg="${_STATE_DIR}/travel-router"
    {
        printf 'ENABLE_WIREGUARD=%s\n' "${ENABLE_WIREGUARD}"
        printf 'WG_INTERFACE=%s\n'    "${WG_INTERFACE}"
        printf 'NTFY_TOPIC=%s\n'      "${NTFY_TOPIC:-}"
    } > "$cfg"

    local patched="${_STATE_DIR}/wireguard-watchdog-patched.sh"
    sed "s|source /etc/default/travel-router|source '${cfg}'|g
         s|/run/lock/wireguard-watchdog.lock|${_STATE_DIR}/lock/wireguard-watchdog.lock|g
         s|/usr/local/bin/notify-router.sh|${MOCK_BIN}/notify-router.sh|g" \
        "$SCRIPT" > "$patched"
    chmod +x "$patched"

    bash "$patched"
}

# ---------------------------------------------------------------------------
# Test 1: ENABLE_WIREGUARD=0 → script exits without touching wg or ip
# ---------------------------------------------------------------------------
@test "wireguard-watchdog: ENABLE_WIREGUARD=0 exits without calling ip or wg" {
    export ENABLE_WIREGUARD="0"

    # Capture any ip / wg call
    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/ip.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/ip"
    chmod +x "${MOCK_BIN}/ip"
    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/wg.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/wg"
    chmod +x "${MOCK_BIN}/wg"

    run _run_watchdog
    [ "$status" -eq 0 ]

    # Neither ip nor wg should have been invoked
    [ ! -f "${MOCK_BIN}/ip.calls" ]
    [ ! -f "${MOCK_BIN}/wg.calls" ]
}

# ---------------------------------------------------------------------------
# Test 2: wg0 interface missing → systemctl restart wg-quick@wg0
# ---------------------------------------------------------------------------
@test "wireguard-watchdog: wg0 missing calls systemctl restart wg-quick@wg0" {
    # ip link show wg0 → exits 1 (interface not found)
    cat > "${MOCK_BIN}/ip" << 'MOCK'
#!/bin/bash
if [[ "$*" == *"link show wg0"* ]]; then
    exit 1
fi
exit 0
MOCK
    chmod +x "${MOCK_BIN}/ip"

    # systemctl: capture calls
    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/systemctl.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/systemctl"
    chmod +x "${MOCK_BIN}/systemctl"

    run _run_watchdog
    [ "$status" -eq 0 ]

    [ -f "${MOCK_BIN}/systemctl.calls" ]
    grep -q "restart.*wg-quick" "${MOCK_BIN}/systemctl.calls"
}

# ---------------------------------------------------------------------------
# Test 3: wg0 exists but is DOWN → systemctl restart wg-quick@wg0
# ---------------------------------------------------------------------------
@test "wireguard-watchdog: wg0 DOWN calls systemctl restart wg-quick@wg0" {
    cat > "${MOCK_BIN}/ip" << 'MOCK'
#!/bin/bash
if [[ "$*" == *"link show wg0"* ]]; then
    printf "4: wg0: <POINTOPOINT,NOARP> mtu 1420 state DOWN\n"
    exit 0
fi
exit 0
MOCK
    chmod +x "${MOCK_BIN}/ip"

    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/systemctl.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/systemctl"
    chmod +x "${MOCK_BIN}/systemctl"

    run _run_watchdog
    [ "$status" -eq 0 ]

    [ -f "${MOCK_BIN}/systemctl.calls" ]
    grep -q "restart.*wg-quick" "${MOCK_BIN}/systemctl.calls"
}

# ---------------------------------------------------------------------------
# Test 4: wg0 UP and recent handshake → no restart
# ---------------------------------------------------------------------------
@test "wireguard-watchdog: wg0 UP with recent handshake produces no restart" {
    cat > "${MOCK_BIN}/ip" << 'MOCK'
#!/bin/bash
printf "4: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420 state UNKNOWN\n"
exit 0
MOCK
    chmod +x "${MOCK_BIN}/ip"

    cat > "${MOCK_BIN}/wg" << 'MOCK'
#!/bin/bash
printf "interface: wg0\n"
printf "  peer: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
printf "    latest handshake: 30 seconds ago\n"
MOCK
    chmod +x "${MOCK_BIN}/wg"

    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/systemctl.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/systemctl"
    chmod +x "${MOCK_BIN}/systemctl"

    run _run_watchdog
    [ "$status" -eq 0 ]

    # systemctl should NOT have been called with restart
    ! grep -q "restart" "${MOCK_BIN}/systemctl.calls" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Test 5: stale handshake (> 3 minutes) → logger records stale handshake
# ---------------------------------------------------------------------------
@test "wireguard-watchdog: stale handshake over 180s is logged" {
    cat > "${MOCK_BIN}/ip" << 'MOCK'
#!/bin/bash
printf "4: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420 state UNKNOWN\n"
exit 0
MOCK
    chmod +x "${MOCK_BIN}/ip"

    cat > "${MOCK_BIN}/wg" << 'MOCK'
#!/bin/bash
printf "interface: wg0\n"
printf "  peer: BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=\n"
printf "    latest handshake: 10 minutes, 5 seconds ago\n"
MOCK
    chmod +x "${MOCK_BIN}/wg"

    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/logger.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/logger"
    chmod +x "${MOCK_BIN}/logger"

    run _run_watchdog
    [ "$status" -eq 0 ]

    [ -f "${MOCK_BIN}/logger.calls" ]
    grep -q "stale" "${MOCK_BIN}/logger.calls"
}

# ---------------------------------------------------------------------------
# Test 6: wg0 interface missing → notification is sent (logger or notify-router.sh)
# ---------------------------------------------------------------------------
@test "wireguard-watchdog: wg0 missing triggers a notification" {
    cat > "${MOCK_BIN}/ip" << 'MOCK'
#!/bin/bash
if [[ "$*" == *"link show wg0"* ]]; then
    exit 1
fi
exit 0
MOCK
    chmod +x "${MOCK_BIN}/ip"

    printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/systemctl.calls"\n' \
        "${MOCK_BIN}" > "${MOCK_BIN}/systemctl"
    chmod +x "${MOCK_BIN}/systemctl"

    run _run_watchdog
    [ "$status" -eq 0 ]

    # Either notify-router.sh or logger should have been called with WireGuard msg
    local notified=0
    grep -qi "WireGuard" "${MOCK_BIN}/notify-router.sh.calls" 2>/dev/null && notified=1
    grep -qi "WireGuard" "${MOCK_BIN}/logger.calls"           2>/dev/null && notified=1
    [ "$notified" -eq 1 ]
}
