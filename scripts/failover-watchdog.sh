#!/bin/bash
# Uplink failover: iPhone USB tether (enx*, metric 100) → Android USB tether (rndis0/usb0, metric 200) → Bluetooth PAN (bnep0, metric 300) → wlan0 (metric 600)
# Runs as a systemd service every 30 seconds

LOGFILE="/var/log/failover-watchdog.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOGFILE"
}

# N-M19: safe notify wrapper — falls back to logger if notify-router.sh is absent
_notify_safe() {
    local msg="$1" priority="${2:-default}"
    if command -v notify-router.sh >/dev/null 2>&1 || [ -x /usr/local/bin/notify-router.sh ]; then
        /usr/local/bin/notify-router.sh "$msg" "$priority" 2>/dev/null || true
    else
        logger -t travel-router "$msg"
    fi
}

# H1: prevent concurrent instances from racing
mkdir -p /run/lock
exec 9>/run/lock/failover-watchdog.lock
flock -n 9 || exit 0

# N-M18: atomic log truncation with mktemp and EXIT trap cleanup
_LOG_TMP=""
_cleanup_log_tmp() { [ -n "$_LOG_TMP" ] && rm -f "$_LOG_TMP"; }
trap _cleanup_log_tmp EXIT

truncate_log() {
    _LOG_TMP=$(mktemp "${LOGFILE}.tmp.XXXXXX")
    tail -n 10000 "$LOGFILE" > "$_LOG_TMP" && mv "$_LOG_TMP" "$LOGFILE" || true
    _LOG_TMP=""
}
truncate_log

# Find active iPhone USB tether interface (enx*)
# N-M3: also match UNKNOWN and DORMANT link states (common on USB gadgets)
get_usb_tether_iface() {
    ip -br link | awk '/^enx/ && /UP|UNKNOWN|DORMANT/ {print $1}' | head -1
}

# Find active Android USB tether interface (RNDIS or CDC-ECM)
# NOTE: usb0 is also the interface name used by the g_ncm USB gadget (when the
# Pi acts as a USB device and the laptop is the host).  On the Pi Zero 2 W there
# is only one USB port, so "USB host mode" (Android tethering into the Pi) and
# "USB gadget mode" (laptop connecting to the Pi) are mutually exclusive — only
# one can be active at a time.  When this function matches usb0 it is always the
# Android-tethering scenario; the gadget/laptop interface will never be up
# simultaneously.
get_android_tether_iface() {
    ip -br link | awk '/^rndis0/ && /UP/ {print $1; exit}
                       /^usb0/  && /UP/ {print $1; exit}' | head -1
}

# Find active Bluetooth PAN tether interface
get_bt_tether_iface() {
    ip -br link | awk '/^bnep0/ && /UP/ {print $1}' | head -1
}

# Find wlan0 if it has a default route
get_wifi_iface() {
    ip route | awk '/default.*wlan0/{print "wlan0"}' | head -1
}

# Get the gateway for a given interface from the default route
# H2: use awk -v variable + string equality to avoid unescaped $iface in regex
get_gateway() {
    local iface=$1
    ip route | awk -v iface="$iface" '/default/{for(i=1;i<=NF;i++){if($i=="dev" && $(i+1)==iface){for(j=1;j<=NF;j++){if($j=="via"){print $(j+1);exit}}}}}' | head -1
}

# Get the current metric for a default route on an interface
get_metric() {
    local iface=$1
    ip route | awk -v iface="$iface" '/default/{for(i=1;i<=NF;i++){if($i=="dev" && $(i+1)==iface){for(j=1;j<=NF;j++){if($j=="metric"){print $(j+1);exit}}}}}' | head -1
}

# Re-add a default route preserving its gateway, with a new metric
set_default_metric() {
    local iface=$1
    local metric=$2
    local gw
    # N-M2: read gateway at the moment of action, not snapshot time
    gw=$(get_gateway "$iface")
    if [ -z "${gw}" ]; then
        log "set_default_metric: no gateway for $iface, skipping route add"
        return 0
    fi
    # Use ip route replace for an atomic metric change (no routing gap)
    if ! ip route replace default via "$gw" dev "$iface" metric "$metric" 2>/dev/null; then
        log "set_default_metric: ip route replace failed for $iface via $gw metric $metric"
    fi
}

# Check if an interface can reach the internet (captive-portal-aware).
# Uses HTTP probes rather than ICMP so that hotel APs that answer ping locally
# are not mistaken for working uplinks.
#
# Probe A: GET http://connectivitycheck.gstatic.com/generate_204
#   → 204  clear internet
#   → 000  no layer-3 connectivity (skip second probe, return fail)
#   → else captive portal or broken — try probe B
#
# Probe B: GET http://detectportal.firefox.com/success.txt
#   → body "success"  clear internet
#   → else            portal or no connectivity
#
# Returns 0 (success) only when at least one probe confirms clear internet.
# Returns 1 for portal, no connectivity, or any error.
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

    # Ambiguous (portal redirect or unexpected code) — try second endpoint
    local body_b
    body_b=$(curl -s -o - -w "" \
        --max-time 5 --interface "$iface" \
        "http://detectportal.firefox.com/success.txt" 2>/dev/null)
    local trimmed
    trimmed=$(printf '%s' "$body_b" | tr -d '\r\n')
    [ "$trimmed" = "success" ] && return 0

    return 1
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

# ── Uplink-change state tracking ─────────────────────────────────────────────
_UPLINK_STATE_DIR="/var/lib/travel-router"
_UPLINK_STATE_FILE="$_UPLINK_STATE_DIR/uplink.state"
mkdir -p "$_UPLINK_STATE_DIR"

_uplink_label() {
    case "$1" in
        enx*) printf "iPhone USB" ;;
        rndis0|usb0) printf "Android USB" ;;
        bnep0) printf "Bluetooth PAN" ;;
        wlan0) printf "WiFi STA" ;;
        *) printf "%s" "$1" ;;
    esac
}

_notify_uplink_change() {
    local curr_uplink="$1"
    local prev_uplink=""
    local _tmp
    [ -f "$_UPLINK_STATE_FILE" ] && prev_uplink=$(cat "$_UPLINK_STATE_FILE")
    # N-H1: always write state file when curr_uplink is non-empty (first-run persistence)
    if [ -n "$curr_uplink" ]; then
        _tmp=$(mktemp "${_UPLINK_STATE_DIR}/uplink.XXXXXX")
        if printf '%s\n' "$curr_uplink" > "$_tmp"; then
            mv "$_tmp" "$_UPLINK_STATE_FILE"
        else
            rm -f "$_tmp"
        fi
    fi
    # Only send notification when there actually was a previous uplink and it changed
    if [ -n "$curr_uplink" ] && [ -n "$prev_uplink" ] && [ "$curr_uplink" != "$prev_uplink" ]; then
        local prev_label curr_label
        prev_label=$(_uplink_label "${prev_uplink:-none}")
        curr_label=$(_uplink_label "$curr_uplink")
        _notify_safe "Uplink: ${prev_label} → ${curr_label}" low
    fi
}
# ─────────────────────────────────────────────────────────────────────────────

USB_TETHER=$(get_usb_tether_iface)
ANDROID_TETHER=$(get_android_tether_iface)
BT_TETHER=$(get_bt_tether_iface)
WIFI=$(get_wifi_iface)

if [ -n "$USB_TETHER" ]; then
    if can_reach_internet "$USB_TETHER"; then
        promote_iface "$USB_TETHER" 100 "USB tether"
        [ -n "$ANDROID_TETHER" ] && promote_iface "$ANDROID_TETHER" 200 "Android tether"
        [ -n "$BT_TETHER" ] && promote_iface "$BT_TETHER" 300 "Bluetooth tether"
        [ -n "$WIFI" ] && promote_iface "wlan0" 600 "WiFi"
        _notify_uplink_change "$USB_TETHER"
        exit 0
    else
        log "USB tether $USB_TETHER is UP but cannot reach internet"
        # N-H2: demote failed interface so it is not chosen as default
        set_default_metric "$USB_TETHER" 900
    fi
fi

if [ -n "$ANDROID_TETHER" ]; then
    if can_reach_internet "$ANDROID_TETHER"; then
        promote_iface "$ANDROID_TETHER" 100 "Android tether"
        [ -n "$BT_TETHER" ] && promote_iface "$BT_TETHER" 300 "Bluetooth tether"
        [ -n "$WIFI" ] && promote_iface "wlan0" 600 "WiFi"
        _notify_uplink_change "$ANDROID_TETHER"
        exit 0
    else
        log "Android tether $ANDROID_TETHER is UP but cannot reach internet"
        # N-H2: demote failed interface so it is not chosen as default
        set_default_metric "$ANDROID_TETHER" 900
    fi
fi

if [ -n "$BT_TETHER" ]; then
    if can_reach_internet "$BT_TETHER"; then
        promote_iface "$BT_TETHER" 100 "Bluetooth tether"
        [ -n "$WIFI" ] && promote_iface "wlan0" 600 "WiFi"
        _notify_uplink_change "$BT_TETHER"
        exit 0
    else
        log "Bluetooth tether $BT_TETHER is UP but cannot reach internet"
        # N-H2: demote failed interface so it is not chosen as default
        set_default_metric "$BT_TETHER" 900
    fi
fi

if [ -n "$WIFI" ]; then
    if can_reach_internet "$WIFI"; then
        promote_iface "wlan0" 100 "WiFi"
        _notify_uplink_change "wlan0"
        exit 0
    fi
fi

log "WARNING: No working uplink found"
