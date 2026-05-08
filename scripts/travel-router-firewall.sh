#!/bin/bash
# Idempotently apply travel-router firewall, TTL, and optional proxy rules.

set -euo pipefail

# Restore FORWARD DROP on unexpected failure so the firewall is never left open
trap 'iptables -P FORWARD DROP 2>/dev/null || true; ip6tables -P FORWARD DROP 2>/dev/null || true' ERR

# Prevent concurrent executions from accumulating duplicate iptables rules
mkdir -p /run/lock
exec 8>/run/lock/travel-router-firewall.lock
flock -x 8

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
    local _tmp
    _tmp=$(mktemp /etc/iptables/rules.v4.XXXXXX)
    iptables-save > "$_tmp" && mv "$_tmp" /etc/iptables/rules.v4 || rm -f "$_tmp"
    _tmp=$(mktemp /etc/iptables/rules.v6.XXXXXX)
    ip6tables-save > "$_tmp" && mv "$_tmp" /etc/iptables/rules.v6 || rm -f "$_tmp"
}

# TTL, hop-limit, DSCP, and hop-by-hop rules are in /etc/nftables.conf.d/travel-router.nft

# FORWARD: set DROP policy BEFORE flush so there is never an open window
# between the flush and the first ACCEPT rule being added.
iptables -P FORWARD DROP
iptables -F FORWARD
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# AP client isolation: prevent clients from reaching each other or the Pi LAN.
iptables -A FORWARD -i uap0 -o uap0 -j DROP

# IPv6 FORWARD: mirror the IPv4 policy so AP clients cannot bypass the VPN
# kill-switch via IPv6 (default ip6tables FORWARD policy is ACCEPT).
ip6tables -P FORWARD DROP
ip6tables -F FORWARD
ip6tables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A FORWARD -i uap0 -o uap0 -j DROP

if [ "$ENABLE_VPN_KILLSWITCH" = "1" ]; then
    # Flush and rebuild chain each run so rules are always current.
    iptables -t filter -N KILL_SWITCH 2>/dev/null || iptables -t filter -F KILL_SWITCH
    iptables -t filter -A KILL_SWITCH -o tailscale0 -j ACCEPT
    iptables -t filter -A KILL_SWITCH -o wg0 -j ACCEPT
    iptables -t filter -A KILL_SWITCH -j DROP
    iptables -A FORWARD -i uap0 -j KILL_SWITCH
    # ip6tables kill-switch mirror
    ip6tables -t filter -N KILL_SWITCH6 2>/dev/null || ip6tables -t filter -F KILL_SWITCH6
    ip6tables -t filter -A KILL_SWITCH6 -o tailscale0 -j ACCEPT
    ip6tables -t filter -A KILL_SWITCH6 -o wg0 -j ACCEPT
    ip6tables -t filter -A KILL_SWITCH6 -j DROP
    ip6tables -A FORWARD -i uap0 -j KILL_SWITCH6
    ip6tables -A FORWARD -i tailscale0 -o uap0 -j ACCEPT
    ip6tables -A FORWARD -i wg0 -o uap0 -j ACCEPT
else
    for _out in wlan0 bnep0 tailscale0 wg0 usb0 rndis0 enx+; do
        iptables -A FORWARD -i uap0 -o "$_out" -j ACCEPT
    done
    # IPv6 FORWARD rules (non-kill-switch path)
    for _uplink in wlan0 bnep0 usb0 rndis0 enx+; do
        ip6tables -A FORWARD -i uap0 -o "$_uplink" -j ACCEPT
        ip6tables -A FORWARD -i "$_uplink" -o uap0 -j ACCEPT
    done
    ip6tables -A FORWARD -i uap0 -o tailscale0 -j ACCEPT
    ip6tables -A FORWARD -i tailscale0 -o uap0 -j ACCEPT
    ip6tables -A FORWARD -i uap0 -o wg0 -j ACCEPT
    ip6tables -A FORWARD -i wg0 -o uap0 -j ACCEPT
fi

# INPUT: block AP clients from Pi admin interfaces.
ipt_add filter INPUT -i uap0 -p tcp --dport 22 -j DROP
ip6t_add filter INPUT -i uap0 -p tcp --dport 22 -j DROP
ipt_add filter INPUT -i uap0 -p tcp --dport 80 -j DROP
ip6t_add filter INPUT -i uap0 -p tcp --dport 80 -j DROP

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
    ip rule show | grep -qE 'fwmark 0x64[[:space:]]+lookup[[:space:]]+100([^0-9]|$)' || \
        ip rule add fwmark 0x64 table 100 priority 100 2>/dev/null || true
else
    ip rule del fwmark 0x64 table 100 2>/dev/null || true
    iptables -t mangle -D PREROUTING -i uap0 -j VPN_DEVICES 2>/dev/null || true
    iptables -t mangle -F VPN_DEVICES 2>/dev/null || true
    iptables -t mangle -X VPN_DEVICES 2>/dev/null || true
fi

if [ "${1:-}" = "--save" ]; then
    save_rules
fi
