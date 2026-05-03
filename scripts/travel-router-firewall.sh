#!/bin/bash
# Idempotently apply travel-router firewall, TTL, and optional proxy rules.

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

ENABLE_HTTP_UA_REWRITE="${ENABLE_HTTP_UA_REWRITE:-0}"
ENABLE_TOR_TRANSPARENT="${ENABLE_TOR_TRANSPARENT:-0}"
ENABLE_VPN_KILLSWITCH="${ENABLE_VPN_KILLSWITCH:-0}"

ipt_add() {
    local table=$1 chain=$2
    shift 2
    iptables -t "$table" -C "$chain" "$@" 2>/dev/null || \
        iptables -t "$table" -A "$chain" "$@"
}

ip6t_add() {
    local table=$1 chain=$2
    shift 2
    ip6tables -t "$table" -C "$chain" "$@" 2>/dev/null || \
        ip6tables -t "$table" -A "$chain" "$@"
}

save_rules() {
    if command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save; then
        return 0
    fi

    mkdir -p /etc/iptables
    iptables-save > /etc/iptables/rules.v4
    ip6tables-save > /etc/iptables/rules.v6
}

# TTL=65 / hop-limit=65: carrier tethering fingerprint mitigation.
for iface in uap0 wlan0 usb0 eth+ enx+; do
    ipt_add mangle POSTROUTING -o "$iface" -j TTL --ttl-set 65
done

for iface in uap0 wlan0 usb0; do
    ip6t_add mangle POSTROUTING -o "$iface" -j HL --hl-set 65
done

# DSCP strip on uplinks to avoid leaking host QoS fingerprints.
for iface in wlan0 usb0 enx+ bnep0; do
    ipt_add mangle POSTROUTING -o "$iface" -j DSCP --set-dscp 0
done

# Drop hop-by-hop extension headers when the kernel module supports matching.
ip6t_add mangle POSTROUTING -o wlan0 -m ipv6header --header hop-by-hop -j DROP 2>/dev/null || true

# FORWARD: flush and rebuild each run — guarantees correct rule ordering.
iptables -F FORWARD
iptables -P FORWARD DROP
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# AP client isolation: prevent clients from reaching each other or the Pi LAN.
iptables -A FORWARD -i uap0 -o uap0 -j DROP

if [ "$ENABLE_VPN_KILLSWITCH" = "1" ]; then
    # Flush and rebuild chain each run so rules are always current.
    iptables -t filter -N KILL_SWITCH 2>/dev/null || iptables -t filter -F KILL_SWITCH
    iptables -t filter -A KILL_SWITCH -o tailscale0 -j RETURN
    iptables -t filter -A KILL_SWITCH -j DROP
    iptables -A FORWARD -i uap0 -j KILL_SWITCH
else
    for _out in wlan0 bnep0 tailscale0 usb0 rndis0 enx+; do
        iptables -A FORWARD -i uap0 -o "$_out" -j ACCEPT
    done
fi
iptables -A FORWARD -i tailscale0 -o uap0 -j ACCEPT

# INPUT: block AP clients from Pi admin interfaces.
ipt_add filter INPUT -i uap0 -p tcp --dport 22 -j DROP
ipt_add filter INPUT -i uap0 -p tcp --dport 80 -j DROP

if [ "$ENABLE_HTTP_UA_REWRITE" = "1" ]; then
    ipt_add nat PREROUTING -i uap0 -p tcp --dport 80 -j REDIRECT --to-port 8118
fi

if [ "$ENABLE_TOR_TRANSPARENT" = "1" ]; then
    TOR_SUBNET="172.16.100.0/24"
    ipt_add nat PREROUTING -s "$TOR_SUBNET" -p udp --dport 53 -j REDIRECT --to-ports 5353
    ipt_add nat PREROUTING -s "$TOR_SUBNET" -p tcp -d 10.0.0.0/8 -j RETURN
    ipt_add nat PREROUTING -s "$TOR_SUBNET" -p tcp -d 172.16.0.0/12 -j RETURN
    ipt_add nat PREROUTING -s "$TOR_SUBNET" -p tcp -d 192.168.0.0/16 -j RETURN
    ipt_add nat PREROUTING -s "$TOR_SUBNET" -p tcp --syn -j REDIRECT --to-ports 9040
fi

ENABLE_PER_DEVICE_VPN="${ENABLE_PER_DEVICE_VPN:-0}"
VPN_DEVICE_MACS="${VPN_DEVICE_MACS:-}"

if [ "$ENABLE_PER_DEVICE_VPN" = "1" ] && [ -n "$VPN_DEVICE_MACS" ]; then
    # Flush and rebuild VPN_DEVICES chain
    iptables -t mangle -N VPN_DEVICES 2>/dev/null || iptables -t mangle -F VPN_DEVICES
    read -ra _macs <<< "$VPN_DEVICE_MACS"
    for _mac in "${_macs[@]}"; do
        iptables -t mangle -A VPN_DEVICES -m mac --mac-source "$_mac" -j MARK --set-mark 0x64
    done
    ipt_add mangle PREROUTING -i uap0 -j VPN_DEVICES

    # Routing table 100: default via tailscale0
    ip route replace default dev tailscale0 table 100 2>/dev/null || true
    # Add ip rule only if not already present
    ip rule show | grep -q "fwmark 0x64 lookup 100" || \
        ip rule add fwmark 0x64 table 100 priority 100 2>/dev/null || true
fi

if [ "${1:-}" = "--save" ]; then
    save_rules
fi
