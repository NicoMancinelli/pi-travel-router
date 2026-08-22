#!/usr/bin/env bats
# Unit tests for scripts/apply-wg-split-tunnel.sh
# Tests CIDR validation, ipset population, mark/policy/route application,
# teardown on disable, and pre-flight failure handling. All dataplane
# commands are mocked; the defaults file is redirected into a temp dir.

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
APPLY_SCRIPT="${SCRIPT_DIR}/scripts/apply-wg-split-tunnel.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"

    export TR_DEFAULTS_FILE="${_STATE_DIR}/travel-router"

    # Knobs consumed by the mock bodies below (reset every test)
    export IP_MOCK_NO_DEV=""
    export IPTABLES_MOCK_STRICT=""
    export IPSET_MOCK_NOSET=""

    mock_cmd_script modprobe 'exit 0'
    mock_cmd_script logger 'echo "$*" >> "$MOCK_BIN/logger.calls"; exit 0'

    # ipset: log everything; every operation succeeds unless NOSET makes
    # `list <name>` fail so the create branch can be exercised
    mock_cmd_script ipset 'echo "$*" >> "$MOCK_BIN/ipset.calls"
if [ "$IPSET_MOCK_NOSET" = "1" ] && [ "$1" = "list" ]; then
    exit 1
fi
exit 0'

    # iptables: log everything; -C succeeds unless STRICT mode forces it to
    # fail (args start with "-t mangle", so -C is $3), exercising -A fallback
    mock_cmd_script iptables 'echo "$*" >> "$MOCK_BIN/iptables.calls"
if [ "$IPTABLES_MOCK_STRICT" = "1" ] && [ "$3" = "-C" ]; then
    exit 1
fi
exit 0'

    # ip: link show succeeds unless IP_MOCK_NO_DEV; rule show is STATEFUL —
    # `rule add` records a show-format line, `rule del` drops it — so the
    # script's own idempotency grep behaves like the kernel would
    mock_cmd_script ip 'echo "$*" >> "$MOCK_BIN/ip.calls"
if [ "$IP_MOCK_NO_DEV" = "1" ] && [ "$1" = "link" ]; then
    exit 1
fi
if [ "$1" = "link" ]; then
    exit 0
fi
if [ "$1" = "rule" ] && [ "$2" = "show" ]; then
    printf "0: from all lookup local\n32766: from all lookup main\n"
    cat "$MOCK_BIN/ip-rules.db" 2>/dev/null || true
    exit 0
fi
if [ "$1" = "rule" ] && [ "$2" = "add" ]; then
    mark=""; tab=""
    prev=""
    for a in "$@"; do
        case "$prev" in
            fwmark) mark="$a" ;;
            table)  tab="$a" ;;
        esac
        prev="$a"
    done
    printf "%s:\tfwmark %s lookup %s\n" "32770" "$mark" "$tab" >> "$MOCK_BIN/ip-rules.db"
    exit 0
fi
if [ "$1" = "rule" ] && [ "$2" = "del" ]; then
    if [ -f "$MOCK_BIN/ip-rules.db" ]; then
        tmp="$(mktemp)"
        grep -v "fwmark 0x3" "$MOCK_BIN/ip-rules.db" > "$tmp" || true
        mv "$tmp" "$MOCK_BIN/ip-rules.db"
    fi
    exit 0
fi
exit 0'
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# Helper: write the defaults file the script sources
_write_defaults() {
    printf '%s\n' "$@" > "${TR_DEFAULTS_FILE}"
}

@test "wg-split-tunnel: disabled by default tears down leftover state and exits 0" {
    _write_defaults '# empty config'
    run bash "${APPLY_SCRIPT}"
    [ "$status" -eq 0 ]
    grep -q "destroy travel_cidr_subnets" "$MOCK_BIN/ipset.calls"
    grep -q "rule del fwmark 0x3 table 201" "$MOCK_BIN/ip.calls"
    [[ "$output" != *"active"* ]]
}

@test "wg-split-tunnel: missing ipset binary skips gracefully" {
    rm -f "${MOCK_BIN}/ipset"
    _write_defaults 'ENABLE_WG_SPLIT_TUNNEL="1"' 'WG_SPLIT_TUNNEL_CIDRS="10.100.0.0/16"'
    run bash "${APPLY_SCRIPT}"
    [ "$status" -eq 0 ]
    grep -qi "ipset not available" "$MOCK_BIN/logger.calls"
}

@test "wg-split-tunnel: empty CIDR list skips without touching dataplane" {
    _write_defaults 'ENABLE_WG_SPLIT_TUNNEL="1"' 'WG_SPLIT_TUNNEL_CIDRS=""'
    run bash "${APPLY_SCRIPT}"
    [ "$status" -eq 0 ]
    [ ! -f "$MOCK_BIN/ipset.calls" ]
    grep -qi "empty" "$MOCK_BIN/logger.calls"
}

@test "wg-split-tunnel: out-of-range prefix length is rejected before applying anything" {
    _write_defaults 'ENABLE_WG_SPLIT_TUNNEL="1"' 'WG_SPLIT_TUNNEL_CIDRS="10.0.0.0/33"'
    run bash "${APPLY_SCRIPT}"
    [ "$status" -eq 1 ]
    [[ "$output" == *"/33"* ]]
    [ ! -f "$MOCK_BIN/ipset.calls" ]
    [ ! -f "$MOCK_BIN/iptables.calls" ]
}

@test "wg-split-tunnel: non-CIDR garbage is rejected before applying anything" {
    _write_defaults 'ENABLE_WG_SPLIT_TUNNEL="1"' 'WG_SPLIT_TUNNEL_CIDRS="example.com 10.0.0.0/8"'
    run bash "${APPLY_SCRIPT}"
    [ "$status" -eq 1 ]
    [[ "$output" == *"example.com"* ]]
    [ ! -f "$MOCK_BIN/ipset.calls" ]
}

@test "wg-split-tunnel: octet above 255 is rejected" {
    _write_defaults 'ENABLE_WG_SPLIT_TUNNEL="1"' 'WG_SPLIT_TUNNEL_CIDRS="256.1.1.0/24"'
    run bash "${APPLY_SCRIPT}"
    [ "$status" -eq 1 ]
    [[ "$output" == *"256.1.1.0/24"* ]]
}

@test "wg-split-tunnel: existing set is flushed and repopulated with configured CIDRs" {
    _write_defaults 'ENABLE_WG_SPLIT_TUNNEL="1"' \
        'WG_SPLIT_TUNNEL_CIDRS="10.100.0.0/16 172.16.5.0/24"' \
        'WG_SPLIT_TUNNEL_DEV="tailscale0"'
    run bash "${APPLY_SCRIPT}"
    [ "$status" -eq 0 ]

    local setcalls iptcalls ipcalls logcalls
    setcalls="$(mock_calls ipset)"
    iptcalls="$(mock_calls iptables)"
    ipcalls="$(mock_calls ip)"
    logcalls="$(cat "$MOCK_BIN/logger.calls")"

    grep -q "flush travel_cidr_subnets" <<< "$setcalls"
    grep -q "add travel_cidr_subnets 10.100.0.0/16 -exist" <<< "$setcalls"
    grep -q "add travel_cidr_subnets 172.16.5.0/24 -exist" <<< "$setcalls"
    [[ "$setcalls" != *"create"* ]]   # set already existed (list mock succeeds)

    grep -q -- "-t mangle -C PREROUTING" <<< "$iptcalls"

    grep -q "rule add fwmark 0x3 table 201 priority 201" <<< "$ipcalls"
    grep -q "route replace default dev tailscale0 table 201" <<< "$ipcalls"

    grep -q "CIDR split tunnel active via tailscale0" <<< "$logcalls"
    grep -q "10.100.0.0/16" <<< "$logcalls"
}

@test "wg-split-tunnel: missing set is created on first run" {
    export IPSET_MOCK_NOSET="1"
    _write_defaults 'ENABLE_WG_SPLIT_TUNNEL="1"' 'WG_SPLIT_TUNNEL_CIDRS="10.7.0.0/16"'
    run bash "${APPLY_SCRIPT}"
    [ "$status" -eq 0 ]
    grep -q "create travel_cidr_subnets hash:net" "$MOCK_BIN/ipset.calls"
    grep -q "Created ipset travel_cidr_subnets" "$MOCK_BIN/logger.calls"
}

@test "wg-split-tunnel: custom egress device wg0 is honoured" {
    _write_defaults 'ENABLE_WG_SPLIT_TUNNEL="1"' \
        'WG_SPLIT_TUNNEL_CIDRS="192.0.2.0/24"' \
        'WG_SPLIT_TUNNEL_DEV="wg0"'
    run bash "${APPLY_SCRIPT}"
    [ "$status" -eq 0 ]
    grep -q "route replace default dev wg0 table 201" "$MOCK_BIN/ip.calls"
    grep -q "active via wg0" "$MOCK_BIN/logger.calls"
}

@test "wg-split-tunnel: re-run does not duplicate mark rule or policy rule" {
    _write_defaults 'ENABLE_WG_SPLIT_TUNNEL="1"' 'WG_SPLIT_TUNNEL_CIDRS="10.8.0.0/16"'
    bash "${APPLY_SCRIPT}"
    bash "${APPLY_SCRIPT}"
    local appends addrules
    appends="$(grep -c -- "-A PREROUTING" "$MOCK_BIN/iptables.calls" || true)"
    addrules="$(grep -c "rule add fwmark" "$MOCK_BIN/ip.calls" || true)"
    [ "$appends" -eq 0 ]
    [ "$addrules" -eq 1 ]
}

@test "wg-split-tunnel: failing -C check falls back to appending the mark rule exactly once per run" {
    export IPTABLES_MOCK_STRICT="1"
    _write_defaults 'ENABLE_WG_SPLIT_TUNNEL="1"' 'WG_SPLIT_TUNNEL_CIDRS="10.8.0.0/16"'
    bash "${APPLY_SCRIPT}"
    bash "${APPLY_SCRIPT}"
    [ "$(grep -c -- "-A PREROUTING" "$MOCK_BIN/iptables.calls")" -eq 2 ]
    [ "$(grep -c -- "-C PREROUTING" "$MOCK_BIN/iptables.calls")" -eq 2 ]
}

@test "wg-split-tunnel: missing egress device aborts before any state is created" {
    export IP_MOCK_NO_DEV="1"
    _write_defaults 'ENABLE_WG_SPLIT_TUNNEL="1"' \
        'WG_SPLIT_TUNNEL_CIDRS="10.9.0.0/16"' \
        'WG_SPLIT_TUNNEL_DEV="wg0"'
    run bash "${APPLY_SCRIPT}"
    [ "$status" -eq 1 ]
    [[ "$output" == *"not present"* ]]
    [ ! -f "$MOCK_BIN/ipset.calls" ]
    [ ! -f "$MOCK_BIN/iptables.calls" ]
}
