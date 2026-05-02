#!/bin/bash
# Uplink failover: prefer iPhone tether (enx*) over Bluetooth PAN (bnep0) over wlan0
# Runs as a systemd service every 30 seconds

LOGFILE="/var/log/failover-watchdog.log"
PING_TARGET="8.8.8.8"
PING_COUNT=2
PING_TIMEOUT=3

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOGFILE"
}

# Find active iPhone USB tether interface (enx*)
get_usb_tether_iface() {
    ip -br link | awk '/^enx/ && /UP/ {print $1}' | head -1
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
get_gateway() {
    local iface=$1
    ip route | awk "/default.*via.*$iface/{print \$3}" | head -1
}

# Get the current metric for a default route on an interface
get_metric() {
    local iface=$1
    ip route | awk "/default.*$iface/{for(i=1;i<=NF;i++){if(\$i==\"metric\"){print \$(i+1);exit}}}" | head -1
}

# Re-add a default route preserving its gateway, with a new metric
set_default_metric() {
    local iface=$1
    local metric=$2
    local gw
    gw=$(get_gateway "$iface")
    ip route del default dev "$iface" 2>/dev/null || true
    if [ -n "$gw" ]; then
        ip route add default via "$gw" dev "$iface" metric "$metric"
    else
        ip route add default dev "$iface" metric "$metric"
    fi
}

# Check if an interface can reach the internet
can_reach_internet() {
    local iface=$1
    ping -c $PING_COUNT -W $PING_TIMEOUT -I "$iface" "$PING_TARGET" > /dev/null 2>&1
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

USB_TETHER=$(get_usb_tether_iface)
BT_TETHER=$(get_bt_tether_iface)
WIFI=$(get_wifi_iface)

if [ -n "$USB_TETHER" ]; then
    if can_reach_internet "$USB_TETHER"; then
        promote_iface "$USB_TETHER" 100 "USB tether"
        [ -n "$BT_TETHER" ] && promote_iface "$BT_TETHER" 300 "Bluetooth tether"
        [ -n "$WIFI" ] && promote_iface "wlan0" 600 "WiFi"
        exit 0
    else
        log "USB tether $USB_TETHER is UP but cannot reach internet"
    fi
fi

if [ -n "$BT_TETHER" ]; then
    if can_reach_internet "$BT_TETHER"; then
        promote_iface "$BT_TETHER" 100 "Bluetooth tether"
        [ -n "$WIFI" ] && promote_iface "wlan0" 600 "WiFi"
        exit 0
    else
        log "Bluetooth tether $BT_TETHER is UP but cannot reach internet"
    fi
fi

if [ -n "$WIFI" ]; then
    if can_reach_internet "$WIFI"; then
        promote_iface "wlan0" 100 "WiFi"
        exit 0
    fi
fi

log "WARNING: No working uplink found"
