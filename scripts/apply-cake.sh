#!/bin/bash
# Apply CAKE qdisc to uplink interfaces for bufferbloat control
# CAKE handles egress queue fairness; TCP BBR handles sender-side congestion
# Run at boot via cake-qdisc.service; also called by start-tether.sh per tether
#
# Tune bandwidth values to ~90% of your actual uplink speed:
#   Visible cellular: 15mbit upload is conservative; increase if speeds allow
#   Hotel WiFi: 50mbit is a generous estimate; adjust down on slow connections

apply_cake() {
    local iface=$1
    local bw=$2
    if ip link show "$iface" > /dev/null 2>&1; then
        tc qdisc replace dev "$iface" root cake bandwidth "$bw" besteffort
        logger "apply-cake: CAKE applied to $iface at $bw"
    fi
}

apply_cake wlan0 50mbit   # hotel/home WiFi uplink
# iPhone tether: CAKE applied per-interface by start-tether.sh at 15mbit
