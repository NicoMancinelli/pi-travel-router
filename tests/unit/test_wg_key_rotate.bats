#!/usr/bin/env bats
# bats file_tags=wg,wireguard,key-rotate
# Unit tests for scripts/wg-key-rotate.sh

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
SCRIPT="${SCRIPT_DIR}/scripts/wg-key-rotate.sh"

# Fixed key values returned by mocked wg genkey / wg pubkey
FAKE_PRIVATE_KEY="dGVzdHByaXZhdGVrZXliYXNlNjRlbmNvZGVkPT0="
FAKE_PUBLIC_KEY="dGVzdHB1YmxpY2tleWJhc2U2NGVuY29kZWQ9PQ=="

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"

    # Redirect WireGuard paths to tmp
    export WG_DIR="${_STATE_DIR}/wireguard"
    mkdir -p "${WG_DIR}"
    export WG_PRIVATE_KEY="${WG_DIR}/private.key"
    export WG_PUBLIC_KEY="${WG_DIR}/public.key"
    export WG_CONF="${WG_DIR}/wg0.conf"
    export NOTIFY="${MOCK_BIN}/notify-router.sh"

    # Create a minimal wg0.conf
    cat > "${WG_CONF}" <<'EOF'
[Interface]
PrivateKey = oldprivatekeyvalue==
ListenPort = 51820
Address = 10.0.0.1/24
EOF

    # Default mocks
    mock_cmd "logger"          "" 0
    mock_cmd "notify-router.sh" "" 0
    mock_cmd "systemctl"       "" 0

    # wg genkey / pubkey mock: write keys and print them
    mock_cmd_script "wg" "
        case \"\$1\" in
            genkey)
                printf '%s\n' '${FAKE_PRIVATE_KEY}'
                ;;
            pubkey)
                printf '%s\n' '${FAKE_PUBLIC_KEY}'
                ;;
            *)
                exit 0
                ;;
        esac
    "
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# Helper: build a patched copy of the script with redirected paths
_build_script() {
    local active="${1:-0}"   # 1 = wg-quick@wg0 is-active returns 0; 0 = returns non-zero
    local tmp="${_STATE_DIR}/wg_key_rotate_test.sh"

    # systemctl mock: is-active → returns based on $active flag
    if [ "${active}" -eq 1 ]; then
        mock_cmd_script "systemctl" '
            printf "%s\n" "$*" >> "${MOCK_BIN}/systemctl.calls"
            if [ "$1" = "is-active" ]; then
                exit 0
            fi
            exit 0
        '
    else
        mock_cmd_script "systemctl" '
            printf "%s\n" "$*" >> "${MOCK_BIN}/systemctl.calls"
            if [ "$1" = "is-active" ]; then
                exit 1
            fi
            exit 0
        '
    fi

    # Patch the script to redirect paths
    sed \
        -e 's|source /etc/default/travel-router.*|true|' \
        -e "s|WG_PRIVATE_KEY=\"/etc/wireguard/private.key\"|WG_PRIVATE_KEY=\"${WG_PRIVATE_KEY}\"|" \
        -e "s|WG_PUBLIC_KEY=\"/etc/wireguard/public.key\"|WG_PUBLIC_KEY=\"${WG_PUBLIC_KEY}\"|" \
        -e "s|WG_CONF=\"/etc/wireguard/wg0.conf\"|WG_CONF=\"${WG_CONF}\"|" \
        -e "s|NOTIFY=\"/usr/local/sbin/notify-router.sh\"|NOTIFY=\"${NOTIFY}\"|" \
        "${SCRIPT}" > "${tmp}"
    chmod +x "${tmp}"
    printf '%s' "${tmp}"
}

# ---------------------------------------------------------------------------
# Test 1: wg genkey and wg pubkey are called (new keypair generated)
# ---------------------------------------------------------------------------
@test "wg-key-rotate: wg genkey and wg pubkey are invoked" {
    # Intercept wg calls and record them
    mock_cmd_script "wg" "
        printf '%s\n' \"\$1\" >> '${MOCK_BIN}/wg.calls'
        case \"\$1\" in
            genkey) printf '%s\n' '${FAKE_PRIVATE_KEY}' ;;
            pubkey) printf '%s\n' '${FAKE_PUBLIC_KEY}'  ;;
        esac
    "
    local script
    script=$(_build_script 0)

    run bash "${script}"
    [ "$status" -eq 0 ]
    grep -q "genkey" "${MOCK_BIN}/wg.calls"
    grep -q "pubkey" "${MOCK_BIN}/wg.calls"
}

# ---------------------------------------------------------------------------
# Test 2: wg0.conf PrivateKey line is updated with new key
# ---------------------------------------------------------------------------
@test "wg-key-rotate: PrivateKey in wg0.conf is updated to new key" {
    local script
    script=$(_build_script 0)

    run bash "${script}"
    [ "$status" -eq 0 ]
    grep -q "PrivateKey = ${FAKE_PRIVATE_KEY}" "${WG_CONF}"
    # Old key must be gone
    run grep -c "oldprivatekeyvalue" "${WG_CONF}"
    [ "$output" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Test 3: /etc/wireguard/private.key is chmod 600 after generation
# ---------------------------------------------------------------------------
@test "wg-key-rotate: private.key is written with mode 600" {
    local script
    script=$(_build_script 0)

    run bash "${script}"
    [ "$status" -eq 0 ]

    # Check permissions: stat output should show 600
    local perms
    perms="$(stat -c '%a' "${WG_PRIVATE_KEY}" 2>/dev/null || stat -f '%Lp' "${WG_PRIVATE_KEY}")"
    [ "$perms" = "600" ]
}

# ---------------------------------------------------------------------------
# Test 4: wg-quick@wg0 active → systemctl restart wg-quick@wg0 is called
# ---------------------------------------------------------------------------
@test "wg-key-rotate: restarts wg-quick@wg0 when service is active" {
    local script
    script=$(_build_script 1)

    run bash "${script}"
    [ "$status" -eq 0 ]
    grep -q "restart wg-quick@wg0" "${MOCK_BIN}/systemctl.calls"
}

# ---------------------------------------------------------------------------
# Test 5: wg-quick@wg0 inactive → restart is NOT called
# ---------------------------------------------------------------------------
@test "wg-key-rotate: does NOT restart wg-quick@wg0 when service is inactive" {
    local script
    script=$(_build_script 0)

    run bash "${script}"
    [ "$status" -eq 0 ]
    # restart must NOT appear in calls
    run grep -c "restart wg-quick@wg0" "${MOCK_BIN}/systemctl.calls" 2>/dev/null || true
    [ "${output:-0}" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Test 6: notify-router.sh is called with the new public key in the message
# ---------------------------------------------------------------------------
@test "wg-key-rotate: notify-router.sh is called with new public key" {
    mock_cmd_script "notify-router.sh" \
        'printf "%s\n" "$*" >> "${MOCK_BIN}/notify.calls"; exit 0'

    local script
    script=$(_build_script 0)

    run bash "${script}"
    [ "$status" -eq 0 ]
    [ -f "${MOCK_BIN}/notify.calls" ]
    grep -q "${FAKE_PUBLIC_KEY}" "${MOCK_BIN}/notify.calls"
}

# ---------------------------------------------------------------------------
# Test 7: Missing wg0.conf — logs warning but still exits 0
# ---------------------------------------------------------------------------
@test "wg-key-rotate: logs warning and exits 0 when wg0.conf is missing" {
    rm -f "${WG_CONF}"

    local script
    script=$(_build_script 0)

    run bash "${script}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Warning"* ]] || [[ "$output" == *"not found"* ]]
}

# ---------------------------------------------------------------------------
# Test 8: New private key is written to private.key file
# ---------------------------------------------------------------------------
@test "wg-key-rotate: new private key is written to the private key file" {
    local script
    script=$(_build_script 0)

    run bash "${script}"
    [ "$status" -eq 0 ]
    [ -f "${WG_PRIVATE_KEY}" ]
    run cat "${WG_PRIVATE_KEY}"
    [ "$output" = "${FAKE_PRIVATE_KEY}" ]
}
