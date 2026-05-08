#!/bin/bash
# install/05-firewall.sh — iptables/ip6tables firewall rules
# Defines run_firewall(). Source this file; do not execute directly.

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
}
