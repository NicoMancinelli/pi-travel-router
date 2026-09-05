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

ENABLE_VPN_KILLSWITCH="${ENABLE_VPN_KILLSWITCH:-0}"
ENABLE_BLOCK_QUIC="${ENABLE_BLOCK_QUIC:-1}"

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
    if iptables-save > "$_tmp" && mv "$_tmp" /etc/iptables/rules.v4; then :; else rm -f "$_tmp"; fi
    _tmp=$(mktemp /etc/iptables/rules.v6.XXXXXX)
    if ip6tables-save > "$_tmp" && mv "$_tmp" /etc/iptables/rules.v6; then :; else rm -f "$_tmp"; fi
}

restore_rules() {
    # Fast path: restore persisted rules to avoid full rebuild on every start
    if command -v netfilter-persistent >/dev/null 2>&1; then
        if netfilter-persistent reload 2>/dev/null; then return 0; fi
    fi
    if [ -f /etc/iptables/rules.v4 ]; then iptables-restore  < /etc/iptables/rules.v4 2>/dev/null || true; fi
    if [ -f /etc/iptables/rules.v6 ]; then ip6tables-restore < /etc/iptables/rules.v6 2>/dev/null || true; fi
}

# If called with --restore, replay saved rules and exit (used by boot service)
if [ "${1:-}" = "--restore" ]; then
    restore_rules
    exit 0
fi

# TTL, hop-limit, DSCP, and hop-by-hop rules are in /etc/nftables.conf.d/travel-router.nft

# FORWARD: set DROP policy BEFORE flush so there is never an open window
# between the flush and the first ACCEPT rule being added.
iptables -P FORWARD DROP
iptables -F FORWARD
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# AP client isolation: prevent clients from reaching each other or the Pi LAN.
iptables -A FORWARD -i uap0 -o uap0 -j DROP

# Carrier stealth (Visible/Verizon): reject QUIC (UDP 443) from AP clients.
# Forces browsers to fall back immediately to TCP TLS 1.3, avoiding cellular UDP throttling,
# DPI packet inspection of unencrypted UDP handshakes, and allowing TCP MSS clamping & BBR to work.
if [ "$ENABLE_BLOCK_QUIC" = "1" ]; then
    iptables -A FORWARD -i uap0 -p udp --dport 443 -j REJECT --reject-with icmp-port-unreachable
fi

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

# NAT: Masquerade outgoing traffic on all uplinks
for _out in wlan0 bnep0 tailscale0 wg0 usb0 rndis0 enx+; do
    ipt_add nat POSTROUTING -o "$_out" -j MASQUERADE
done

# DNS interception: redirect client DNS queries on uap0 to local dnsmasq (:53).
# Prevents carrier DPI (Visible/Verizon) from profiling desktop OS domains (Windows Update, telemetry)
# when clients configure public DNS (e.g. 8.8.8.8) or bypass DHCP DNS.
ipt_add nat PREROUTING -i uap0 -p udp --dport 53 -j REDIRECT --to-ports 53
ipt_add nat PREROUTING -i uap0 -p tcp --dport 53 -j REDIRECT --to-ports 53
ip6t_add nat PREROUTING -i uap0 -p udp --dport 53 -j REDIRECT --to-ports 53 2>/dev/null || true
ip6t_add nat PREROUTING -i uap0 -p tcp --dport 53 -j REDIRECT --to-ports 53 2>/dev/null || true

# NTP interception: redirect client NTP (UDP 123) to local NTP daemon (chrony).
# Prevents desktop OS queries (time.windows.com, time.apple.com) from profiling client devices over cellular uplinks.
ipt_add nat PREROUTING -i uap0 -p udp --dport 123 -j REDIRECT --to-ports 123
ip6t_add nat PREROUTING -i uap0 -p udp --dport 123 -j REDIRECT --to-ports 123 2>/dev/null || true

# Carrier bypass defense-in-depth (Visible/Verizon): enforce TTL=65, DSCP 0, and MSS clamping in iptables
for _out in wlan0 bnep0 usb0 rndis0 enx+; do
    ipt_add mangle POSTROUTING -o "$_out" -j TTL --ttl-set 65 2>/dev/null || true
    ipt_add mangle POSTROUTING -o "$_out" -j DSCP --set-dscp 0 2>/dev/null || true
    ipt_add mangle FORWARD -o "$_out" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || true
done

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
