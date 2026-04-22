#!/bin/bash
# Uplink failover: prefer iPhone tether (enx*) over wlan0
# Runs as a systemd service every 30 seconds

LOGFILE="/var/log/failover-watchdog.log"
PING_TARGET="8.8.8.8"
PING_COUNT=2
PING_TIMEOUT=3

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOGFILE"
}

# Find active iPhone tether interface (enx*)
get_tether_iface() {
    ip -br link | awk '/^enx/ && /UP/ {print $1}' | head -1
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

TETHER=$(get_tether_iface)
WIFI=$(get_wifi_iface)

if [ -n "$TETHER" ]; then
    if can_reach_internet "$TETHER"; then
        current_metric=$(get_metric "$TETHER")
        if [ "$current_metric" != "100" ]; then
            set_default_metric "$TETHER" 100
            log "Tether $TETHER set as primary uplink (metric 100)"
        fi
        if [ -n "$WIFI" ]; then
            current_wifi=$(get_metric "wlan0")
            if [ "$current_wifi" != "600" ]; then
                set_default_metric "wlan0" 600
                log "wlan0 demoted to fallback (metric 600)"
            fi
        fi
    else
        log "Tether $TETHER is UP but cannot reach internet — staying on current route"
    fi
elif [ -n "$WIFI" ]; then
    if can_reach_internet "$WIFI"; then
        current_metric=$(get_metric "wlan0")
        if [ "$current_metric" != "100" ]; then
            set_default_metric "wlan0" 100
            log "No tether — wlan0 promoted to primary (metric 100)"
        fi
    else
        log "WARNING: No working uplink found"
    fi
fi
