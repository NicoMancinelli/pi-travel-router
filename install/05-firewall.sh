#!/bin/bash
# install/05-firewall.sh — iptables/ip6tables firewall rules
# Defines run_firewall(). Source this file; do not execute directly.

run_firewall() {
    section "Firewall — TTL, DSCP, isolation rules"

    # I-H4: firewall applied AFTER tailscaled is enabled/started so tailscale0
    # interface exists when iptables rules that reference it are saved.
    if is_dry_run; then
        log "[DRY-RUN] /usr/local/bin/travel-router-firewall.sh --save"
    else
        /usr/local/bin/travel-router-firewall.sh --save
    fi

    ok "Firewall rules applied and saved"
}
