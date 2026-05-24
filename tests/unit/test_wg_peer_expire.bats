#!/usr/bin/env bats
# bats file_tags=wg,wireguard,peer-expire
# Unit tests for scripts/wg-peer-expire.sh

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
WG_EXPIRE_SCRIPT="${SCRIPT_DIR}/scripts/wg-peer-expire.sh"

# Fixed "today" used by the mocked date command
FIXED_TODAY="2025-06-15"
YESTERDAY="2025-06-14"
TOMORROW="2025-06-16"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"

    # All writable paths go inside _STATE_DIR
    export WG_CONF="${_STATE_DIR}/wg0.conf"
    export LOG_DIR="${_STATE_DIR}/log"

    mkdir -p "${LOG_DIR}"

    # Mock date to return a fixed today
    mock_cmd_script "date" "printf '%s\n' '${FIXED_TODAY}'"

    # Default mocks (silent / pass-through)
    mock_cmd "logger"          "" 0
    mock_cmd "ip"              "" 1    # wg0 down by default
    mock_cmd "wg"              "" 0
    mock_cmd "notify-router.sh" "" 0
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# Helper: build a patched copy of the script with redirected paths
_build_script() {
    local tmp="${_STATE_DIR}/wg_peer_expire_test.sh"
    sed \
        -e "s|WG_CONF=\"/etc/wireguard/wg0.conf\"|WG_CONF=\"${WG_CONF}\"|" \
        -e "s|LOG_DIR=\"/var/log/travel-router\"|LOG_DIR=\"${LOG_DIR}\"|" \
        -e "s|NOTIFY_SCRIPT=\"/usr/local/sbin/notify-router.sh\"|NOTIFY_SCRIPT=\"${MOCK_BIN}/notify-router.sh\"|" \
        -e "s|mktemp /etc/wireguard/wg0.conf.XXXXXX|mktemp \"${_STATE_DIR}/wg0.conf.XXXXXX\"|" \
        "${WG_EXPIRE_SCRIPT}" > "${tmp}"
    chmod +x "${tmp}"
    printf '%s' "${tmp}"
}

# ---------------------------------------------------------------------------
# Test 1: Peer with an expired date is removed from wg0.conf
# ---------------------------------------------------------------------------
@test "wg-peer-expire: expired peer is removed from wg0.conf" {
    cat > "${WG_CONF}" <<EOF
[Interface]
Address = 10.0.0.1/24
PrivateKey = base64privatekey==

[Peer]
PublicKey = expiredpeer1234567890==
AllowedIPs = 10.0.0.2/32
# expires: ${YESTERDAY}
EOF

    local script
    script=$(_build_script)
    run bash "${script}"
    [ "$status" -eq 0 ]
    run grep -c "expiredpeer1234567890" "${WG_CONF}"
    [ "$output" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Test 2: Peer with a future expiry date is NOT removed
# ---------------------------------------------------------------------------
@test "wg-peer-expire: future-dated peer is preserved in wg0.conf" {
    cat > "${WG_CONF}" <<EOF
[Interface]
Address = 10.0.0.1/24
PrivateKey = base64privatekey==

[Peer]
PublicKey = futurepeer1234567890==
AllowedIPs = 10.0.0.3/32
# expires: ${TOMORROW}
EOF

    local script
    script=$(_build_script)
    run bash "${script}"
    [ "$status" -eq 0 ]
    grep -q "futurepeer1234567890" "${WG_CONF}"
}

# ---------------------------------------------------------------------------
# Test 3: Peer with no expires comment is NOT removed
# ---------------------------------------------------------------------------
@test "wg-peer-expire: peer without expires comment is preserved" {
    cat > "${WG_CONF}" <<EOF
[Interface]
Address = 10.0.0.1/24
PrivateKey = base64privatekey==

[Peer]
PublicKey = neverexpirespeer123==
AllowedIPs = 10.0.0.4/32
EOF

    local script
    script=$(_build_script)
    run bash "${script}"
    [ "$status" -eq 0 ]
    grep -q "neverexpirespeer123" "${WG_CONF}"
}

# ---------------------------------------------------------------------------
# Test 4: Multiple peers — only expired one is removed, others preserved
# ---------------------------------------------------------------------------
@test "wg-peer-expire: only expired peer is removed when multiple peers exist" {
    cat > "${WG_CONF}" <<EOF
[Interface]
Address = 10.0.0.1/24
PrivateKey = base64privatekey==

[Peer]
PublicKey = expiredpeerAAA==
AllowedIPs = 10.0.0.2/32
# expires: ${YESTERDAY}

[Peer]
PublicKey = activepeerBBB==
AllowedIPs = 10.0.0.3/32
# expires: ${TOMORROW}

[Peer]
PublicKey = neverexpiresccc==
AllowedIPs = 10.0.0.4/32
EOF

    local script
    script=$(_build_script)
    run bash "${script}"
    [ "$status" -eq 0 ]
    # Expired peer gone
    run grep -c "expiredpeerAAA" "${WG_CONF}"
    [ "$output" -eq 0 ]
    # Active peer preserved
    grep -q "activepeerBBB" "${WG_CONF}"
    # No-expiry peer preserved
    grep -q "neverexpiresccc" "${WG_CONF}"
}

# ---------------------------------------------------------------------------
# Test 5: wg0 interface UP → wg set wg0 peer <key> remove is called
# ---------------------------------------------------------------------------
@test "wg-peer-expire: calls wg set remove when wg0 interface is up" {
    cat > "${WG_CONF}" <<EOF
[Interface]
Address = 10.0.0.1/24
PrivateKey = base64privatekey==

[Peer]
PublicKey = expiredpeerWGUP==
AllowedIPs = 10.0.0.2/32
# expires: ${YESTERDAY}
EOF

    # ip link show wg0 → exit 0 (interface is up)
    mock_cmd_script "ip" 'exit 0'
    # wg: record args
    mock_cmd_script "wg" 'printf "%s\n" "$*" >> "${MOCK_BIN}/wg.calls"; exit 0'

    local script
    script=$(_build_script)
    run bash "${script}"
    [ "$status" -eq 0 ]
    grep -q "set wg0 peer expiredpeerWGUP== remove" "${MOCK_BIN}/wg.calls"
}

# ---------------------------------------------------------------------------
# Test 6: wg0 interface DOWN → wg set is NOT called
# ---------------------------------------------------------------------------
@test "wg-peer-expire: does NOT call wg set remove when wg0 is down" {
    cat > "${WG_CONF}" <<EOF
[Interface]
Address = 10.0.0.1/24
PrivateKey = base64privatekey==

[Peer]
PublicKey = expiredpeerWGDOWN==
AllowedIPs = 10.0.0.2/32
# expires: ${YESTERDAY}
EOF

    # ip link show wg0 → exit 1 (interface absent)
    mock_cmd_script "ip" 'exit 1'
    # wg: record if called
    mock_cmd_script "wg" 'printf "%s\n" "$*" >> "${MOCK_BIN}/wg.calls"; exit 0'

    local script
    script=$(_build_script)
    run bash "${script}"
    [ "$status" -eq 0 ]
    # wg.calls must not exist at all, or must not contain a set command
    if [ -f "${MOCK_BIN}/wg.calls" ]; then
        run grep -c "set wg0 peer" "${MOCK_BIN}/wg.calls"
        [ "$output" -eq 0 ]
    fi
}

# ---------------------------------------------------------------------------
# Test 7: Empty wg0.conf (no peers) — exits 0 cleanly
# ---------------------------------------------------------------------------
@test "wg-peer-expire: empty wg0.conf exits 0 cleanly" {
    printf '' > "${WG_CONF}"

    local script
    script=$(_build_script)
    run bash "${script}"
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Test 8: wg0.conf with only [Interface] section — no peers removed, exits 0
# ---------------------------------------------------------------------------
@test "wg-peer-expire: interface-only conf exits 0 with no changes" {
    cat > "${WG_CONF}" <<EOF
[Interface]
Address = 10.0.0.1/24
PrivateKey = base64privatekey==
ListenPort = 51820
EOF

    mock_cmd_script "wg" 'printf "%s\n" "$*" >> "${MOCK_BIN}/wg.calls"; exit 0'

    local script
    script=$(_build_script)
    run bash "${script}"
    [ "$status" -eq 0 ]
    grep -q "\[Interface\]" "${WG_CONF}"
    # wg set should NOT have been called
    if [ -f "${MOCK_BIN}/wg.calls" ]; then
        run grep -c "set wg0 peer" "${MOCK_BIN}/wg.calls"
        [ "$output" -eq 0 ]
    fi
}

# ---------------------------------------------------------------------------
# Test 9: Missing wg0.conf — exits 0 (guard clause)
# ---------------------------------------------------------------------------
@test "wg-peer-expire: missing wg0.conf exits 0 silently" {
    rm -f "${WG_CONF}"

    local script
    script=$(_build_script)
    run bash "${script}"
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Test 10: Peer expired exactly on TODAY is removed (boundary condition)
# ---------------------------------------------------------------------------
@test "wg-peer-expire: peer expiring exactly today is removed" {
    cat > "${WG_CONF}" <<EOF
[Interface]
Address = 10.0.0.1/24
PrivateKey = base64privatekey==

[Peer]
PublicKey = todaypeer1234567890==
AllowedIPs = 10.0.0.5/32
# expires: ${FIXED_TODAY}
EOF

    local script
    script=$(_build_script)
    run bash "${script}"
    [ "$status" -eq 0 ]
    run grep -c "todaypeer1234567890" "${WG_CONF}"
    [ "$output" -eq 0 ]
}
