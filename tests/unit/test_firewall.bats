#!/usr/bin/env bats
# Unit tests for scripts/travel-router-firewall.sh
# Mocks iptables and ip6tables to capture rule additions, then verifies
# the correct rules are emitted.  No root access required.

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
FIREWALL_SCRIPT="${SCRIPT_DIR}/scripts/travel-router-firewall.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"

    # Call log files for iptables / ip6tables
    export IPT_LOG="${_STATE_DIR}/iptables.log"
    export IP6T_LOG="${_STATE_DIR}/ip6tables.log"

    # Mock iptables: log every invocation; succeed on all checks (-C)
    mock_cmd_script "iptables" '
        printf "%s\n" "$*" >> "${IPT_LOG}"
        exit 0
    '

    # Mock ip6tables: log every invocation
    mock_cmd_script "ip6tables" '
        printf "%s\n" "$*" >> "${IP6T_LOG}"
        exit 0
    '

    # Other commands the script may call
    mock_cmd "ip"                  ""  0
    mock_cmd "flock"               ""  0
    mock_cmd "netfilter-persistent" "" 1   # not present → falls back to iptables-save
    mock_cmd "iptables-save"       ""  0
    mock_cmd "ip6tables-save"      ""  0

    # Ensure the lock dir is writable
    mkdir -p "${_STATE_DIR}/run/lock"
    export RUN_LOCK_DIR="${_STATE_DIR}/run/lock"

    # Create a minimal /etc/default/travel-router override
    export TRAVEL_ROUTER_ENV="${_STATE_DIR}/travel-router"
    printf '# empty\n' > "${TRAVEL_ROUTER_ENV}"
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# ---------------------------------------------------------------------------
# Helper: run the firewall script with optional env overrides
# ---------------------------------------------------------------------------
_run_firewall() {
    # We must patch the lock path and the source line.
    # Use python3 to avoid sed quoting pitfalls with paths containing special chars.
    local env_overrides="${1:-}"
    local tmp_script="${_STATE_DIR}/firewall_test.sh"
    local lock_dir="${_STATE_DIR}/run/lock"

    python3 - "${FIREWALL_SCRIPT}" "${tmp_script}" "${lock_dir}" "${TRAVEL_ROUTER_ENV}" <<'PYEOF'
import sys, re
src, dst, lock_dir, env_file = sys.argv[1:]
text = open(src).read()
# Redirect lock directory to temp path
text = text.replace('mkdir -p /run/lock', f'mkdir -p "{lock_dir}"')
text = re.sub(r'exec 8>/run/lock/\S+', f'exec 8>"{lock_dir}/firewall.lock"', text)
# Remove flock (no-op in tests)
text = re.sub(r'flock -x 8\b[^\n]*', '', text)
# Redirect config source to temp env file
text = re.sub(r'source /etc/default/travel-router\S*',
              f'source "{env_file}" 2>/dev/null || true', text)
open(dst, 'w').write(text)
PYEOF
    chmod +x "${tmp_script}"

    # shellcheck disable=SC2086
    env ${env_overrides:-} bash "${tmp_script}"
}

# ---------------------------------------------------------------------------
# Test 1: FORWARD policy is set to DROP for both IPv4 and IPv6
# ---------------------------------------------------------------------------
@test "firewall: sets FORWARD policy to DROP for iptables and ip6tables" {
    _run_firewall

    grep -q -- "-P FORWARD DROP" "${IPT_LOG}"
    grep -q -- "-P FORWARD DROP" "${IP6T_LOG}"
}

# ---------------------------------------------------------------------------
# Test 2: Non-kill-switch path adds FORWARD rules for uap0 → tailscale0 and wg0
# ---------------------------------------------------------------------------
@test "firewall: adds FORWARD ACCEPT rules for uap0->tailscale0 and uap0->wg0 (no kill-switch)" {
    printf 'ENABLE_VPN_KILLSWITCH=0\n' > "${TRAVEL_ROUTER_ENV}"

    _run_firewall

    # Should see: -A FORWARD -i uap0 -o tailscale0 -j ACCEPT
    grep -q -- "-A FORWARD -i uap0 -o tailscale0 -j ACCEPT" "${IPT_LOG}"
    # And for wg0
    grep -q -- "-A FORWARD -i uap0 -o wg0 -j ACCEPT" "${IPT_LOG}"
}

# ---------------------------------------------------------------------------
# Test 3: Kill-switch path creates KILL_SWITCH chain allowing only tailscale0/wg0
# ---------------------------------------------------------------------------
@test "firewall: creates KILL_SWITCH chain with tailscale0 and wg0 ACCEPT when enabled" {
    printf 'ENABLE_VPN_KILLSWITCH=1\n' > "${TRAVEL_ROUTER_ENV}"

    _run_firewall

    # Kill-switch chain setup
    grep -q "KILL_SWITCH" "${IPT_LOG}"
    grep -q -- "-A KILL_SWITCH -o tailscale0 -j ACCEPT" "${IPT_LOG}"
    grep -q -- "-A KILL_SWITCH -o wg0 -j ACCEPT" "${IPT_LOG}"
    # Final DROP in chain
    grep -q -- "-A KILL_SWITCH -j DROP" "${IPT_LOG}"
}

# ---------------------------------------------------------------------------
# Test 4: Kill-switch path mirrors rules to ip6tables (KILL_SWITCH6)
# ---------------------------------------------------------------------------
@test "firewall: mirrors KILL_SWITCH rules to ip6tables as KILL_SWITCH6" {
    printf 'ENABLE_VPN_KILLSWITCH=1\n' > "${TRAVEL_ROUTER_ENV}"

    _run_firewall

    grep -q "KILL_SWITCH6" "${IP6T_LOG}"
    grep -q -- "-A KILL_SWITCH6 -o tailscale0 -j ACCEPT" "${IP6T_LOG}"
    grep -q -- "-A KILL_SWITCH6 -o wg0 -j ACCEPT" "${IP6T_LOG}"
    grep -q -- "-A KILL_SWITCH6 -j DROP" "${IP6T_LOG}"
}

# ---------------------------------------------------------------------------
# Test 5: AP client isolation rule is added (uap0 → uap0 DROP)
# ---------------------------------------------------------------------------
@test "firewall: adds client-isolation DROP rule for uap0->uap0" {
    _run_firewall

    grep -q -- "-A FORWARD -i uap0 -o uap0 -j DROP" "${IPT_LOG}"
    grep -q -- "-A FORWARD -i uap0 -o uap0 -j DROP" "${IP6T_LOG}"
}

# ---------------------------------------------------------------------------
# Test 6: INPUT rules block SSH (22) and HTTP (80) from uap0
# ---------------------------------------------------------------------------
@test "firewall: blocks SSH and HTTP on INPUT from uap0" {
    _run_firewall

    grep -q -- "--dport 22 -j DROP" "${IPT_LOG}"
    grep -q -- "--dport 80 -j DROP" "${IPT_LOG}"
    grep -q -- "--dport 22 -j DROP" "${IP6T_LOG}"
    grep -q -- "--dport 80 -j DROP" "${IP6T_LOG}"
}

# ---------------------------------------------------------------------------
# Test 7: IPv6 non-kill-switch path adds FORWARD rules for uplink interfaces
# ---------------------------------------------------------------------------
@test "firewall: IPv6 adds bidirectional FORWARD rules for standard uplinks" {
    printf 'ENABLE_VPN_KILLSWITCH=0\n' > "${TRAVEL_ROUTER_ENV}"

    _run_firewall

    # uap0 → wlan0 and wlan0 → uap0
    grep -q -- "-A FORWARD -i uap0 -o wlan0 -j ACCEPT" "${IP6T_LOG}"
    grep -q -- "-A FORWARD -i wlan0 -o uap0 -j ACCEPT" "${IP6T_LOG}"
}

# ---------------------------------------------------------------------------
# Test 8: ESTABLISHED/RELATED conntrack rule is added first in FORWARD
# ---------------------------------------------------------------------------
@test "firewall: adds ESTABLISHED,RELATED conntrack rule to FORWARD" {
    _run_firewall

    grep -q -- "--ctstate ESTABLISHED,RELATED -j ACCEPT" "${IPT_LOG}"
    grep -q -- "--ctstate ESTABLISHED,RELATED -j ACCEPT" "${IP6T_LOG}"
}
