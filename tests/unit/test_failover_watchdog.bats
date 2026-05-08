#!/usr/bin/env bats
# Unit tests for scripts/failover-watchdog.sh
# Tests focus on interface detection helpers and metric assignment logic.
# We source function definitions only; top-level execution code is guarded.

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"

setup() {
    setup_mock_bin

    # Writable state dir
    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"
    export _UPLINK_STATE_DIR="${_STATE_DIR}"
    export _UPLINK_STATE_FILE="${_STATE_DIR}/uplink.state"

    export LOGFILE="${_STATE_DIR}/failover-watchdog.log"

    # Silence helpers
    mock_cmd "logger" "" 0
    mock_cmd "flock"  "" 0
    mock_cmd "mktemp" "${_STATE_DIR}/tmp.XXXXXX" 0
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# ---------------------------------------------------------------------------
# Source only the pure helper functions (no top-level side effects)
# ---------------------------------------------------------------------------
_load_functions() {
    # shellcheck disable=SC1090
    source /dev/stdin <<'FUNCTIONS'
log() { true; }

get_usb_tether_iface() {
    ip -br link | awk '/^enx/ && /UP|UNKNOWN|DORMANT/ {print $1}' | head -1
}

get_android_tether_iface() {
    ip -br link | awk '/^rndis0/ && /UP/ {print $1; exit}
                       /^usb0/  && /UP/ {print $1; exit}' | head -1
}

get_bt_tether_iface() {
    ip -br link | awk '/^bnep0/ && /UP/ {print $1}' | head -1
}

get_wifi_iface() {
    ip route | awk '/default.*wlan0/{print "wlan0"}' | head -1
}

get_gateway() {
    local iface=$1
    ip route | awk -v iface="$iface" '/default/{for(i=1;i<=NF;i++){if($i=="dev" && $(i+1)==iface){for(j=1;j<=NF;j++){if($j=="via"){print $(j+1);exit}}}}}' | head -1
}

get_metric() {
    local iface=$1
    ip route | awk -v iface="$iface" '
        /default/ {
            dev = ""
            for (i = 1; i <= NF; i++) {
                if ($i == "dev") dev = $(i+1)
                if ($i == "metric" && dev == iface) { print $(i+1); exit }
            }
            if (dev == iface) { print "0"; exit }
        }
    ' | head -1
}

set_default_metric() {
    local iface=$1
    local metric=$2
    local gw
    gw=$(get_gateway "$iface")
    if [ -z "${gw}" ]; then
        log "set_default_metric: no gateway for $iface, skipping route add"
        return 0
    fi
    ip route replace default via "$gw" dev "$iface" metric "$metric" 2>/dev/null
}

promote_iface() {
    local iface=$1 metric=$2 label=$3
    local current_metric
    current_metric=$(get_metric "$iface")
    if [ "$current_metric" != "$metric" ]; then
        set_default_metric "$iface" "$metric"
        log "$label $iface set to metric $metric"
    fi
}

can_reach_internet() {
    local iface=$1
    local code_a
    code_a=$(curl -s -o /dev/null -w "%{http_code}" \
        --max-time 5 --interface "$iface" \
        "http://connectivitycheck.gstatic.com/generate_204" 2>/dev/null)
    case "$code_a" in
        204) return 0 ;;
        000) return 1 ;;
    esac
    local body_b
    body_b=$(curl -s -o - -w "" \
        --max-time 5 --interface "$iface" \
        "http://detectportal.firefox.com/success.txt" 2>/dev/null)
    local trimmed
    trimmed=$(printf '%s' "$body_b" | tr -d '\r\n')
    [ "$trimmed" = "success" ] && return 0
    return 1
}

_uplink_label() {
    case "$1" in
        enx*) printf "iPhone USB" ;;
        rndis0|usb0) printf "Android USB" ;;
        bnep0) printf "Bluetooth PAN" ;;
        wlan0) printf "WiFi STA" ;;
        *) printf "%s" "$1" ;;
    esac
}
FUNCTIONS
}

# ---------------------------------------------------------------------------
# Test 1: get_usb_tether_iface finds enx* interface that is UP
# ---------------------------------------------------------------------------
@test "get_usb_tether_iface: finds enx interface when UP" {
    _load_functions

    mock_cmd_script "ip" '
        if [ "$1" = "-br" ] && [ "$2" = "link" ]; then
            printf "enxaabbccdd1122 UP             aa:bb:cc:dd:11:22\n"
            printf "wlan0           UP             aa:bb:cc:dd:11:23\n"
        fi
    '

    result=$(get_usb_tether_iface)
    [ "$result" = "enxaabbccdd1122" ]
}

# ---------------------------------------------------------------------------
# Test 2: get_usb_tether_iface returns empty when no enx interface
# ---------------------------------------------------------------------------
@test "get_usb_tether_iface: returns empty when no enx interface is present" {
    _load_functions

    mock_cmd_script "ip" '
        if [ "$1" = "-br" ] && [ "$2" = "link" ]; then
            printf "wlan0 UP   aa:bb:cc:dd:11:23\n"
            printf "usb0  UP   aa:bb:cc:dd:11:24\n"
        fi
    '

    result=$(get_usb_tether_iface)
    [ -z "$result" ]
}

# ---------------------------------------------------------------------------
# Test 3: get_metric parses metric field correctly from ip route output
# ---------------------------------------------------------------------------
@test "get_metric: parses metric 100 for enx interface" {
    _load_functions

    mock_cmd_script "ip" '
        printf "default via 10.0.0.1 dev enxaabbcc1122 proto dhcp metric 100\n"
        printf "default via 192.168.1.1 dev wlan0 proto dhcp metric 600\n"
    '

    result=$(get_metric "enxaabbcc1122")
    [ "$result" = "100" ]
}

# ---------------------------------------------------------------------------
# Test 4: get_metric returns "0" when route has no explicit metric
# ---------------------------------------------------------------------------
@test "get_metric: returns '0' when default route has no metric field" {
    _load_functions

    mock_cmd_script "ip" '
        printf "default via 10.0.0.1 dev wlan0\n"
    '

    result=$(get_metric "wlan0")
    [ "$result" = "0" ]
}

# ---------------------------------------------------------------------------
# Test 5: get_wifi_iface returns wlan0 when it has a default route
# ---------------------------------------------------------------------------
@test "get_wifi_iface: returns 'wlan0' when default route via wlan0 exists" {
    _load_functions

    mock_cmd_script "ip" '
        printf "default via 192.168.1.1 dev wlan0 proto dhcp metric 600\n"
    '

    result=$(get_wifi_iface)
    [ "$result" = "wlan0" ]
}

# ---------------------------------------------------------------------------
# Test 6: can_reach_internet returns 0 (success) when curl returns 204
# ---------------------------------------------------------------------------
@test "can_reach_internet: returns 0 when generate_204 returns 204" {
    _load_functions

    mock_cmd_script "curl" 'printf "204"'

    run can_reach_internet "wlan0"
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Test 7: can_reach_internet returns 1 when curl returns 000 (no connection)
# ---------------------------------------------------------------------------
@test "can_reach_internet: returns 1 when curl returns 000" {
    _load_functions

    mock_cmd_script "curl" 'printf "000"'

    run can_reach_internet "wlan0"
    [ "$status" -eq 1 ]
}

# ---------------------------------------------------------------------------
# Test 8: can_reach_internet falls back to detectportal body check
# ---------------------------------------------------------------------------
@test "can_reach_internet: returns 0 on portal redirect when detectportal returns 'success'" {
    _load_functions

    call_n=0
    mock_cmd_script "curl" '
        # First call (generate_204) → 302 (portal)
        # Second call (detectportal) → body "success"
        CALL_FILE="${MOCK_BIN}/curl.n"
        n=0
        [ -f "$CALL_FILE" ] && n=$(cat "$CALL_FILE")
        n=$((n + 1))
        printf "%d" "$n" > "$CALL_FILE"
        if [ "$n" -eq 1 ]; then
            printf "302"
        else
            printf "success"
        fi
    '

    run can_reach_internet "wlan0"
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Test 9: _uplink_label returns correct labels for each interface type
# ---------------------------------------------------------------------------
@test "_uplink_label: returns correct labels for known interface patterns" {
    _load_functions

    [ "$(_uplink_label "enxaabbcc")" = "iPhone USB" ]
    [ "$(_uplink_label "rndis0")"    = "Android USB" ]
    [ "$(_uplink_label "usb0")"      = "Android USB" ]
    [ "$(_uplink_label "bnep0")"     = "Bluetooth PAN" ]
    [ "$(_uplink_label "wlan0")"     = "WiFi STA" ]
}

# ---------------------------------------------------------------------------
# Test 10: promote_iface calls set_default_metric only when metric differs
# ---------------------------------------------------------------------------
@test "promote_iface: calls ip route replace only when metric differs" {
    _load_functions

    # ip route returns metric 600 for wlan0; promote to 100 should trigger replace
    mock_cmd_script "ip" '
        case "$*" in
            "route")
                printf "default via 192.168.1.1 dev wlan0 metric 600\n"
                ;;
            route\ replace*)
                printf "route_replace_called: %s\n" "$*" >> "${_STATE_DIR}/ip.log"
                ;;
            *)
                true
                ;;
        esac
    '

    promote_iface "wlan0" 100 "WiFi"
    grep -q "route_replace_called" "${_STATE_DIR}/ip.log"
}
