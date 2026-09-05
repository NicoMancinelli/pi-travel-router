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
        clone-mac.sh; do
        install_file "scripts/$script" "/usr/local/bin/$script" 755
        ok "  $script"
    done

    ok "Failover/watchdog scripts installed"

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
