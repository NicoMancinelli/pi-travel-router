#!/usr/bin/env bats
# bats file_tags=privacy,profile,apply-privacy-profile
# Unit tests for scripts/apply-privacy-profile.sh

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
APPLY_SCRIPT="${SCRIPT_DIR}/scripts/apply-privacy-profile.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"

    # All writable/readable paths redirected into tmp
    export PROFILE_DIR_ETC="${_STATE_DIR}/profiles-etc"
    export PROFILE_DIR_SHARE="${_STATE_DIR}/profiles-share"
    export STATE_DIR="${_STATE_DIR}/state"
    export LOG_DIR="${_STATE_DIR}/log"

    mkdir -p "${PROFILE_DIR_ETC}" "${PROFILE_DIR_SHARE}" "${STATE_DIR}" "${LOG_DIR}"

    # Standard mocks — iptables outputs a dummy rule with the marker so the
    # grep-in-pipefail step doesn't kill the script
    mock_cmd_script "iptables" '
        if [ "$1" = "-t" ] && [ "$2" = "nat" ] && [ "$3" = "-S" ]; then
            printf -- "-A PREROUTING -j ACCEPT -m comment --comment travel-router-profile\n"
        fi
        exit 0
    '
    mock_cmd "systemctl"       "" 0
    mock_cmd "tailscale"       "" 0
    mock_cmd "notify-router.sh" "" 0
    mock_cmd "resolvconf"      "" 0
    mock_cmd "logger"          "" 0
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# Helper: build a minimal YAML profile file in PROFILE_DIR_ETC
_write_yaml() {
    local profile="$1"
    local yaml="$2"
    printf '%s\n' "${yaml}" > "${PROFILE_DIR_ETC}/${profile}.yaml"
}

# Helper: build a patched copy of the script with all system paths redirected
_build_script() {
    local tmp="${_STATE_DIR}/apply_privacy_profile_test.sh"
    local resolv_tmp="${_STATE_DIR}/etc/resolv.conf"
    local resolv_backup_tmp="${_STATE_DIR}/etc/resolv.conf.travel-router-backup"
    local torrc_tmp="${_STATE_DIR}/etc/tor/torrc"
    mkdir -p "${_STATE_DIR}/etc/tor"
    # Create a dummy resolv.conf so cp doesn't fail
    printf "nameserver 8.8.8.8\n" > "${resolv_tmp}"
    # The script uses `readonly` for most constants; patch with sed
    sed \
        -e "s|readonly PROFILE_DIR_ETC=\"/etc/travel-router/privacy-profiles\"|PROFILE_DIR_ETC=\"${PROFILE_DIR_ETC}\"|" \
        -e "s|readonly PROFILE_DIR_SHARE=\"/usr/local/share/travel-router/privacy-profiles\"|PROFILE_DIR_SHARE=\"${PROFILE_DIR_SHARE}\"|" \
        -e "s|readonly STATE_DIR=\"/var/lib/travel-router\"|STATE_DIR=\"${STATE_DIR}\"|" \
        -e "s|readonly LOG_DIR=\"/var/log/travel-router\"|LOG_DIR=\"${LOG_DIR}\"|" \
        -e "s|readonly FIREWALL_SCRIPT=\"/usr/local/sbin/travel-router-firewall.sh\"|FIREWALL_SCRIPT=\"${MOCK_BIN}/travel-router-firewall.sh\"|" \
        -e "s|readonly NOTIFY_SCRIPT=\"/usr/local/sbin/notify-router.sh\"|NOTIFY_SCRIPT=\"${MOCK_BIN}/notify-router.sh\"|" \
        -e "s|readonly ACTIVE_FILE=.*|ACTIVE_FILE=\"${STATE_DIR}/active-profile\"|" \
        -e "s|readonly PREV_FILE=.*|PREV_FILE=\"${STATE_DIR}/previous-profile\"|" \
        -e "s|readonly KS_MARKER=.*|KS_MARKER=\"${STATE_DIR}/kill-switch-active\"|" \
        -e "s|readonly REVERT_PID_FILE=.*|REVERT_PID_FILE=\"${STATE_DIR}/profile-revert.pid\"|" \
        -e "s|readonly LOG_FILE=.*|LOG_FILE=\"${LOG_DIR}/privacy-profile.log\"|" \
        -e "s|RESOLV=\"/etc/resolv.conf\"|RESOLV=\"${resolv_tmp}\"|" \
        -e "s|RESOLV_BACKUP=\"/etc/resolv.conf.travel-router-backup\"|RESOLV_BACKUP=\"${resolv_backup_tmp}\"|" \
        -e "s|TORRC=\"/etc/tor/torrc\"|TORRC=\"${torrc_tmp}\"|" \
        "${APPLY_SCRIPT}" > "${tmp}"
    chmod +x "${tmp}"
    printf '%s' "${tmp}"
}

# ---------------------------------------------------------------------------
# Test 1: No arguments → exits non-zero, prints usage
# ---------------------------------------------------------------------------
@test "apply-privacy-profile: no arguments prints usage and exits non-zero" {
    local script
    script=$(_build_script)
    run bash "${script}"
    [ "$status" -ne 0 ]
    [[ "$output" == *"Usage"* ]]
}

# ---------------------------------------------------------------------------
# Test 2: Invalid profile name → exits non-zero with error message
# ---------------------------------------------------------------------------
@test "apply-privacy-profile: invalid profile name exits non-zero with error" {
    local script
    script=$(_build_script)
    run bash "${script}" "nosuchprofile"
    [ "$status" -ne 0 ]
    [[ "$output" == *"Usage"* ]] || [[ "$output" == *"unknown profile"* ]]
}

# ---------------------------------------------------------------------------
# Test 3: Missing profile YAML → exits non-zero with error message
# ---------------------------------------------------------------------------
@test "apply-privacy-profile: missing profile YAML exits non-zero" {
    # vpn-only is a valid name but no YAML file present
    local script
    script=$(_build_script)
    run bash "${script}" "vpn-only"
    [ "$status" -ne 0 ]
    [[ "$output" == *"not found"* ]] || [[ "$output" == *"YAML"* ]] || [[ "$output" == *"ERROR"* ]]
}

# ---------------------------------------------------------------------------
# Test 4: vpn-only profile → systemctl start wg-quick@wg0 is called
# ---------------------------------------------------------------------------
@test "apply-privacy-profile: vpn-only profile starts wg-quick@wg0" {
    _write_yaml "vpn-only" "$(cat <<'YAML'
vpn: true
adblock: false
tor: false
kill_switch: false
revert_after: 0
dns: system
YAML
)"
    mock_cmd_script "systemctl" \
        'printf "%s\n" "$*" >> "${MOCK_BIN}/systemctl.calls"; exit 0'

    local script
    script=$(_build_script)
    run bash "${script}" "vpn-only"
    [ "$status" -eq 0 ]
    grep -q "start wg-quick@wg0" "${MOCK_BIN}/systemctl.calls"
}

# ---------------------------------------------------------------------------
# Test 5: adblock-only profile → systemctl stop wg-quick@wg0 is called (vpn=false)
# ---------------------------------------------------------------------------
@test "apply-privacy-profile: adblock-only profile stops wg-quick@wg0" {
    _write_yaml "adblock-only" "$(cat <<'YAML'
vpn: false
adblock: true
tor: false
kill_switch: false
revert_after: 0
dns: adguard
YAML
)"
    mock_cmd_script "systemctl" \
        'printf "%s\n" "$*" >> "${MOCK_BIN}/systemctl.calls"; exit 0'

    local script
    script=$(_build_script)
    run bash "${script}" "adblock-only"
    [ "$status" -eq 0 ]
    grep -q "stop wg-quick@wg0" "${MOCK_BIN}/systemctl.calls"
}

# ---------------------------------------------------------------------------
# Test 6: direct profile — neither wg nor tor started; revert timer spawned
# ---------------------------------------------------------------------------
@test "apply-privacy-profile: direct profile spawns revert timer and stops wg" {
    _write_yaml "direct" "$(cat <<'YAML'
vpn: false
adblock: false
tor: false
kill_switch: false
revert_after: 600
dns: system
YAML
)"
    mock_cmd_script "systemctl" \
        'printf "%s\n" "$*" >> "${MOCK_BIN}/systemctl.calls"; exit 0'
    # sleep: record args if called from background revert timer
    mock_cmd_script "sleep" \
        'printf "%s\n" "$*" >> "${MOCK_BIN}/sleep.calls"; exit 0'

    local script
    script=$(_build_script)
    run bash "${script}" "direct"
    [ "$status" -eq 0 ]

    # WireGuard should be STOPPED (vpn: false)
    grep -q "stop wg-quick@wg0" "${MOCK_BIN}/systemctl.calls"

    # Tor should NOT have been started (tor: false)
    run grep -c "start tor\|restart tor" "${MOCK_BIN}/systemctl.calls" 2>/dev/null || true
    [ "${output:-0}" -eq 0 ]

    # A background revert PID file should exist
    [ -f "${STATE_DIR}/profile-revert.pid" ]
}

# ---------------------------------------------------------------------------
# Test 7: tor profile with tor not installed → warning logged, exits 0
# ---------------------------------------------------------------------------
@test "apply-privacy-profile: tor profile with tor absent logs warning and exits 0" {
    _write_yaml "tor" "$(cat <<'YAML'
vpn: false
adblock: false
tor: true
kill_switch: false
revert_after: 0
dns: system
YAML
)"
    mock_cmd_script "systemctl" \
        'printf "%s\n" "$*" >> "${MOCK_BIN}/systemctl.calls"; exit 0'

    # Ensure 'tor' is not in the mock bin so command -v tor fails
    rm -f "${MOCK_BIN}/tor"

    local script
    script=$(_build_script)
    run bash "${script}" "tor"
    [ "$status" -eq 0 ]
    [[ "$output" == *"tor not installed"* ]] || [[ "$output" == *"WARNING"* ]]
}

# ---------------------------------------------------------------------------
# Test 8: Active profile is written to active-profile after successful apply
# ---------------------------------------------------------------------------
@test "apply-privacy-profile: active profile file is written after apply" {
    _write_yaml "vpn-only" "$(cat <<'YAML'
vpn: true
adblock: false
tor: false
kill_switch: false
revert_after: 0
dns: system
YAML
)"
    mock_cmd_script "systemctl" 'exit 0'

    local script
    script=$(_build_script)
    run bash "${script}" "vpn-only"
    [ "$status" -eq 0 ]
    [ -f "${STATE_DIR}/active-profile" ]
    run cat "${STATE_DIR}/active-profile"
    [ "$output" = "vpn-only" ]
}

# ---------------------------------------------------------------------------
# Test 9: Previous profile is saved before apply
# ---------------------------------------------------------------------------
@test "apply-privacy-profile: previous profile is saved to previous-profile file" {
    _write_yaml "adblock-only" "$(cat <<'YAML'
vpn: false
adblock: true
tor: false
kill_switch: false
revert_after: 0
dns: adguard
YAML
)"
    # Simulate that vpn-only was previously active
    echo "vpn-only" > "${STATE_DIR}/active-profile"

    mock_cmd_script "systemctl" 'exit 0'

    local script
    script=$(_build_script)
    run bash "${script}" "adblock-only"
    [ "$status" -eq 0 ]
    [ -f "${STATE_DIR}/previous-profile" ]
    run cat "${STATE_DIR}/previous-profile"
    [ "$output" = "vpn-only" ]
}
