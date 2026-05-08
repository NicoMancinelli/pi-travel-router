#!/bin/bash
# install/06-failover.sh — policy routing, watchdog scripts, CAKE auto-tuning
# Defines run_failover(). Source this file; do not execute directly.

run_failover() {
    section "Failover watchdog scripts"

    # Install all watchdog/failover scripts
    for script in \
        start-tether.sh stop-tether.sh \
        failover-watchdog.sh wan-watchdog.sh captive-check.sh \
        notify-router.sh apply-cake.sh \
        start-bt-tether.sh stop-bt-tether.sh \
        clone-mac.sh \
        tailscale-watchdog.sh \
        wireguard-watchdog.sh; do
        install_file "scripts/$script" "/usr/local/bin/$script" 755
        ok "  $script"
    done

    ok "Failover/watchdog scripts installed"

    # ── CAKE auto-tuning ────────────────────────────────────────────────────────
    section "CAKE bandwidth auto-tuning"

    install_file scripts/tune-cake.sh /usr/local/bin/tune-cake.sh 755

    if [[ "${ENABLE_CAKE_AUTOTUNE:-0}" = "1" ]]; then
        run_or_dry env DEBIAN_FRONTEND=noninteractive apt-get install -y speedtest-cli 2>/dev/null || true
        run_or_dry systemctl enable --now tune-cake.timer 2>/dev/null || true
        ok "CAKE auto-tune enabled — weekly speedtest adjusts wlan0 CAKE bandwidth"
    else
        systemctl disable tune-cake.timer 2>/dev/null || true
        ok "CAKE auto-tune disabled (set ENABLE_CAKE_AUTOTUNE=1 to activate)"
    fi

    # ── Per-client bandwidth fairness (CAKE per-host) ────────────────────────────
    section "Per-client bandwidth fairness (CAKE per-host)"

    local _DEFAULTS_FILE="/etc/default/travel-router"

    if [[ "${ENABLE_CLIENT_QOS:-0}" = "1" ]]; then
        _safe_write_conf "AP_CLIENT_BANDWIDTH" "${AP_CLIENT_BANDWIDTH:-unlimited}" "$_DEFAULTS_FILE"
        if [[ -x /usr/local/bin/apply-cake.sh ]]; then
            /usr/local/bin/apply-cake.sh 2>&1 || warn "CAKE apply failed (will retry at boot)"
        fi
        cat > /etc/systemd/system/apply-cake.service << 'EOF'
[Unit]
Description=Apply CAKE qdisc on AP interface
After=hostapd.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/apply-cake.sh
[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        run_or_dry systemctl enable apply-cake.service 2>/dev/null || true
        ok "CAKE per-host enabled on uap0 (cap: ${AP_CLIENT_BANDWIDTH:-unlimited})"
    else
        systemctl disable apply-cake.service 2>/dev/null || true
        ok "Per-client QoS disabled (set ENABLE_CLIENT_QOS=1 to activate)"
    fi

    # ── Per-device Tailscale routing ─────────────────────────────────────────────
    section "Per-device Tailscale routing"

    if [[ "${ENABLE_PER_DEVICE_VPN:-0}" = "1" ]]; then
        if [[ -z "${VPN_DEVICE_MACS:-}" ]]; then
            warn "ENABLE_PER_DEVICE_VPN=1 but VPN_DEVICE_MACS is empty"
            warn "  Add MACs to VPN_DEVICE_MACS in /etc/default/travel-router"
        else
            ok "Per-device VPN routing enabled for: ${VPN_DEVICE_MACS}"
        fi
    else
        ok "Per-device VPN routing disabled (set ENABLE_PER_DEVICE_VPN=1 + VPN_DEVICE_MACS)"
    fi
}
