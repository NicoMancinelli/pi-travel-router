#!/bin/bash
# install/05-firewall.sh — iptables/ip6tables firewall rules
# Defines run_firewall(). Source this file; do not execute directly.

_setup_guest_firewall() {
    # Called only when ENABLE_GUEST_NETWORK=1.
    # Assigns the guest gateway IP and adds isolation iptables rules.
    local wan_if
    wan_if=$(ip route show default 2>/dev/null | awk '/^default/ {print $5; exit}')
    wan_if="${wan_if:-wlan0}"

    # Assign gateway IP to guest interface
    run_or_dry ip addr add 192.168.5.1/24 dev uap1 2>/dev/null || true

    # Allow DHCP and DNS from guest subnet
    run_or_dry iptables -A INPUT -i uap1 -p udp --dport 67 -j ACCEPT
    run_or_dry iptables -A INPUT -i uap1 -p udp --dport 53 -j ACCEPT

    # Forward guest traffic to WAN
    run_or_dry iptables -A FORWARD -i uap1 -o "${wan_if}" -j ACCEPT
    run_or_dry iptables -A FORWARD -i "${wan_if}" -o uap1 \
        -m state --state ESTABLISHED,RELATED -j ACCEPT

    # Block guest → primary AP and vice versa
    run_or_dry iptables -A FORWARD -i uap1 -o uap0 -j DROP
    run_or_dry iptables -A FORWARD -i uap0 -o uap1 -j DROP

    # Block guest → router management interfaces
    run_or_dry iptables -A INPUT -i uap1 -p tcp --dport 8080 -j DROP
    run_or_dry iptables -A INPUT -i uap1 -p tcp --dport 22 -j DROP

    # NAT masquerade for guest subnet
    run_or_dry iptables -t nat -A POSTROUTING -s 192.168.5.0/24 \
        -o "${wan_if}" -j MASQUERADE

    ok "Guest firewall rules applied (gateway 192.168.5.1, WAN=${wan_if})"
}

run_firewall() {
    section "Firewall — TTL, DSCP, isolation, optional proxy rules"

    # I-H4: firewall applied AFTER tailscaled is enabled/started so tailscale0
    # interface exists when iptables rules that reference it are saved.
    if is_dry_run; then
        log "[DRY-RUN] /usr/local/bin/travel-router-firewall.sh --save"
    else
        /usr/local/bin/travel-router-firewall.sh --save
    fi

    ok "Firewall rules applied and saved"

    # ── privoxy — optional User-Agent normalization ──────────────────────────────
    section "privoxy — optional HTTP User-Agent normalization"

    install_file config/privoxy-user.action /etc/privoxy/user.action 644
    if [[ "${ENABLE_HTTP_UA_REWRITE:-0}" = "1" ]]; then
        run_or_dry systemctl enable --now privoxy 2>/dev/null || true
        ok "privoxy configured and enabled"
    else
        systemctl disable --now privoxy 2>/dev/null || true
        ok "privoxy installed but disabled by default"
    fi

    # ── Guest network firewall ─────────────────────────────────────────────────
    section "Guest network firewall"
    if [[ "${ENABLE_GUEST_NETWORK:-0}" = "1" ]]; then
        _setup_guest_firewall
    else
        ok "Guest network disabled — skipping guest firewall rules"
    fi
}
