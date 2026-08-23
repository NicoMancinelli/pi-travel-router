#!/usr/bin/env bats
# Unit tests for scripts/apply-split-tunnel.sh
# Domain-based split tunnel: ipset populated by dnsmasq, mangle MARK fwmark
# 0x2, policy rule priority 200, routing table 200 via tailscale0.
# All dataplane commands are mocked; the defaults file path is sed-patched.

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
SCRIPT="${SCRIPT_DIR}/scripts/apply-split-tunnel.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"
    export CFG="${_STATE_DIR}/travel-router"
    printf 'ENABLE_SPLIT_TUNNEL="0"\nSPLIT_TUNNEL_DOMAINS=""\n' > "$CFG"

    export IP_MOCK_NO_DEV=""
    export IPTABLES_MOCK_STRICT=""
    export IPSET_MOCK_MISSING=""
    export IPSET_MOCK_NOSET=""

    mock_cmd_script modprobe 'exit 0'
    mock_cmd_script logger 'echo "$*" >> "$MOCK_BIN/logger.calls"; exit 0'

    # ipset: log everything; every op succeeds unless the binary is "missing"
    if [ "$IPSET_MOCK_MISSING" != "1" ]; then
        mock_cmd_script ipset 'echo "$*" >> "$MOCK_BIN/ipset.calls"
if [ "$IPSET_MOCK_NOSET" = "1" ] && [ "$1" = "list" ]; then
    exit 1
fi
exit 0'
    fi

    # iptables: -C succeeds unless STRICT forces it to fail (args start with
    # "-t mangle", so -C is $3) — exercises the -A fallback branch
    mock_cmd_script iptables 'echo "$*" >> "$MOCK_BIN/iptables.calls"
if [ "$IPTABLES_MOCK_STRICT" = "1" ] && [ "$3" = "-C" ]; then
    exit 1
fi
exit 0'

    # ip: stateful rule db so the script idempotency grep sees added rules;
    # link checks fail when IP_MOCK_NO_DEV is set
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
            fwmark)          mark="$a" ;;
            table|lookup)    tab="$a" ;;
        esac
        prev="$a"
    done
    printf "%s:\tfwmark %s lookup %s\n" "200" "$mark" "$tab" >> "$MOCK_BIN/ip-rules.db"
    exit 0
fi
exit 0'
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# Build a patched copy: redirect the sourced defaults file into the temp dir
_run_tunnel() {
    local patched="${_STATE_DIR}/patched.sh"
    sed "s|source /etc/default/travel-router|source '${CFG}'|g" "$SCRIPT" > "$patched"
    bash "$patched"
}

@test "split-tunnel: disabled tears down leftover state and exits 0" {
    run _run_tunnel
    [ "$status" -eq 0 ]
    grep -q "destroy vpn_domains" "$MOCK_BIN/ipset.calls"
    grep -q "rule del fwmark 0x2 table 200" "$MOCK_BIN/ip.calls"
    [[ "$output" != *"active"* ]]
}

@test "split-tunnel: missing ipset binary skips gracefully" {
    rm -f "${MOCK_BIN}/ipset"
    printf 'ENABLE_SPLIT_TUNNEL="1"\nSPLIT_TUNNEL_DOMAINS="a.com"\n' > "$CFG"
    run _run_tunnel
    [ "$status" -eq 0 ]
    grep -qi "ipset not available" "$MOCK_BIN/logger.calls"
}

@test "split-tunnel: empty domain list skips without touching dataplane" {
    printf 'ENABLE_SPLIT_TUNNEL="1"\nSPLIT_TUNNEL_DOMAINS=""\n' > "$CFG"
    run _run_tunnel
    [ "$status" -eq 0 ]
    [ ! -f "$MOCK_BIN/ipset.calls" ]
    grep -qi "empty" "$MOCK_BIN/logger.calls"
}

@test "split-tunnel: missing tailscale0 aborts with error" {
    export IP_MOCK_NO_DEV="1"
    printf 'ENABLE_SPLIT_TUNNEL="1"\nSPLIT_TUNNEL_DOMAINS="a.com b.example.com"\n' > "$CFG"
    run _run_tunnel
    [ "$status" -eq 1 ]
    grep -qi "tailscale0 not present" "$MOCK_BIN/logger.calls"
}

@test "split-tunnel: creates set, marks domains, adds policy rule and route" {
    printf 'ENABLE_SPLIT_TUNNEL="1"\nSPLIT_TUNNEL_DOMAINS="mybank.com work.example.com"\n' > "$CFG"
    run _run_tunnel
    [ "$status" -eq 0 ]

    local iptcalls ipcalls logcalls
    iptcalls="$(mock_calls iptables)"
    ipcalls="$(mock_calls ip)"
    logcalls="$(cat "$MOCK_BIN/logger.calls")"

    # dnsmasq owns set population; the script only checks the mark rule (-C)
    grep -q -- "-t mangle -C PREROUTING -m set --match-set vpn_domains dst -j MARK --set-mark 0x2" <<< "$iptcalls"
    grep -q "rule add fwmark 0x2 lookup 200 priority 200" <<< "$ipcalls"
    grep -q "route replace default dev tailscale0 table 200" <<< "$ipcalls"
    grep -q "Split tunnel active" <<< "$logcalls"
    grep -q "mybank.com work.example.com" <<< "$logcalls"
}

@test "split-tunnel: creates the vpn_domains set when absent" {
    export IPSET_MOCK_NOSET="1"
    printf 'ENABLE_SPLIT_TUNNEL="1"\nSPLIT_TUNNEL_DOMAINS="a.com"\n' > "$CFG"
    sed -i 's|if \[ "$IPSET_MOCK_MISSING" = "1" \] && \[ "$1" = "list" \]; then|if [ "$IPSET_MOCK_NOSET" = "1" ] \&\& [ "$1" = "list" ]; then|' /dev/null 2>/dev/null || true
    run _run_tunnel
    [ "$status" -eq 0 ]
    grep -q "create vpn_domains hash:ip timeout 7200 maxelem 65536" "$MOCK_BIN/ipset.calls"
}

@test "split-tunnel: re-run does not duplicate mark rule or policy rule" {
    printf 'ENABLE_SPLIT_TUNNEL="1"\nSPLIT_TUNNEL_DOMAINS="a.com"\n' > "$CFG"
    _run_tunnel
    _run_tunnel
    local appends addrules
    appends="$(grep -c -- "-A PREROUTING" "$MOCK_BIN/iptables.calls" || true)"
    addrules="$(grep -c "rule add fwmark" "$MOCK_BIN/ip.calls" || true)"
    [ "$appends" -eq 0 ]
    [ "$addrules" -eq 1 ]
}
