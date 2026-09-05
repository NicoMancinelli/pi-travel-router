#!/bin/bash
# install/04-dns.sh — DNS stack: stubby (DoT) and AdGuard Home
# Defines run_dns(). Source this file; do not execute directly.

run_dns() {
    # ── stubby — DNS-over-TLS ───────────────────────────────────────────────────
    section "stubby — DNS-over-TLS"

    mkdir -p /etc/stubby
    install_file config/stubby.yml /etc/stubby/stubby.yml 644

    if [[ "${ENABLE_DOT:-0}" = "1" ]]; then
        install_file config/dnsmasq-dot.conf /etc/dnsmasq.d/dot.conf
        sed -i "s|127\.0\.0\.1#5300|127.0.0.1#${DOT_PORT:-5300}|" /etc/dnsmasq.d/dot.conf
        run_or_dry systemctl enable --now stubby 2>/dev/null || true
        ok "DNS-over-TLS enabled: dnsmasq → stubby → Cloudflare/Quad9"
    else
        systemctl disable --now stubby 2>/dev/null || true
        ok "DNS-over-TLS installed but disabled (set ENABLE_DOT=1 to activate)"
    fi
}
