#!/bin/bash
# install/10-finalize.sh — version stamp, repo copy, and install summary
# Defines run_finalize(). Source this file; do not execute directly.

run_finalize() {
    # ── Version stamp ────────────────────────────────────────────────────────────
    section "Version stamp"
    local _INSTALLED_VERSION
    _INSTALLED_VERSION="$(cat "${REPO}/VERSION" 2>/dev/null || echo "unknown")"
    echo "$_INSTALLED_VERSION" > /etc/travel-router-version
    mkdir -p /usr/local/share/travel-router
    cp "${REPO}/install.sh" /usr/local/share/travel-router/install.sh
    chmod 755 /usr/local/share/travel-router/install.sh
    ok "Installed version: $_INSTALLED_VERSION"

    # ── Installation summary ─────────────────────────────────────────────────────
    section "Installation complete"

    local _AP_GATEWAY="${AP_GATEWAY:-10.3.141.1}"
    local _AP_SUBNET="${AP_SUBNET:-10.3.141.0/24}"

    echo ""
    echo "  Summary of what was installed:"
    echo "    • RaspAP web UI (http://${_AP_GATEWAY} after boot)"
    echo "    • AP SSID: ${AP_SSID:-TravelRouter}  (on uap0, ${_AP_SUBNET})"
    echo "    • USB gadget: usb0 → 192.168.7.1  (active after reboot)"
    echo "    • iPhone USB tether: udev auto-detect (enx*, metric 100)"
    echo "    • Android USB tether: udev auto-detect (rndis0/usb0, metric 200)"
    echo "    • Bluetooth tether: set IPHONE_BT_MAC in /etc/default/travel-router"
    echo "    • Uplink failover watchdog: 30s timer"
    echo "    • WAN watchdog + captive portal detection: 60s timer"
    echo "    • TTL=65 + DSCP strip (Visible carrier bypass)"
    echo "    • DNS-over-TLS: ${ENABLE_DOT:-0}  (stubby → Cloudflare/Quad9)"
    echo "    • AdGuard Home: ${ENABLE_ADGUARD:-0}  (web UI: http://${_AP_GATEWAY}:3000)"
    echo "    • VPN kill switch: ${ENABLE_VPN_KILLSWITCH:-0}  (AP traffic blocked if Tailscale drops)"
    echo "    • privoxy: HTTP User-Agent normalization (${ENABLE_HTTP_UA_REWRITE:-0})"
    echo "    • Tor: transparent proxy (${ENABLE_TOR_TRANSPARENT:-0})"
    echo "    • Threat intel blocklist: daily timer installed, loading enabled=${ENABLE_BLOCKLISTS:-0}"
    echo "    • Auto security updates: ${ENABLE_AUTO_UPDATES:-0}  (unattended-upgrades, reboot 03:30)"
    echo "    • Auto-update: weekly check (Sun 03:00) — run manually: sudo update-router.sh"
    echo "    • Tailscale watchdog: 5-min peer health check + ntfy alerts"
    echo "    • Daily digest: 08:00 ntfy push (uptime, uplink, Tailscale, AP clients)"
    echo "    • Per-client QoS: ${ENABLE_CLIENT_QOS:-0}  (CAKE per-host on uap0)"
    echo "    • CAKE auto-tune: ${ENABLE_CAKE_AUTOTUNE:-0}  (weekly speedtest → adjusts wlan0 CAKE bandwidth)"
    echo "    • Per-device VPN: ${ENABLE_PER_DEVICE_VPN:-0}  (set VPN_DEVICE_MACS in /etc/default/travel-router)"
    echo "    • Domain split tunnel: ${ENABLE_SPLIT_TUNNEL:-0}  (domains via Tailscale: ${SPLIT_TUNNEL_DOMAINS:-none})"
    echo "    • SSH 2FA (TOTP): ${ENABLE_2FA:-0}  (run: sudo -u \$(logname) setup-2fa.sh to configure)"
    echo "    • WAN metric management: ${ENABLE_WAN_METRICS:-1}  (enx*=100 rndis0=200 bnep0=300 wlan0=600)"
    echo "    • Bandwidth dashboard: ${ENABLE_BANDWIDTH_DASHBOARD:-0}  (http://${_AP_GATEWAY}/bandwidth.html)"
    echo "    • Prometheus node exporter: ${ENABLE_PROMETHEUS_EXPORTER:-0}  (:9100/metrics via Tailscale)"
    echo "    • UPS monitor (PiSugar): ${ENABLE_UPS_MONITOR:-0}  (shutdown at ${UPS_SHUTDOWN_THRESHOLD:-10}%)"
    echo "    • WireGuard VPN: ${ENABLE_WIREGUARD:-0}  (wg0, port ${WG_LISTEN_PORT:-51820}; public key: $(cat /etc/wireguard/wg0.pub 2>/dev/null || echo 'n/a'))"
    echo "    • Run 'sudo travel-status' for a one-shot status summary"
    echo "    • Run 'sudo travel-tui' for the interactive management TUI"
    echo "    • Installed version: $_INSTALLED_VERSION  (cat /etc/travel-router-version)"
    echo "    • Tailscale: subnet router for ${_AP_SUBNET}"
    echo "    • Tailscale control: ${HEADSCALE_URL:-Tailscale cloud (login.tailscale.com)}"
    echo "    • TCP BBR + CAKE qdisc (bufferbloat control)"
    echo "    • log2ram: /var/log in RAM"
    echo "    • Hardware watchdog: BCM2835 — reboots if kernel locks up (active after reboot)"
    echo "    • Log rotation: daily, 7-day retention for wan-watchdog.log"
    echo "    • SSH hardening: PermitRootLogin no, MaxAuthTries 3${SSH_ADMIN_KEY:+, key auth only (password disabled)}"
    echo "    • MAC randomization: wlan0 at boot"
    echo "    • mDNS reflector: ${ENABLE_AVAHI_REFLECTOR:-0}  (AirPrint/AirPlay/NAS over Tailscale)"
    echo "    • AP schedule: ${ENABLE_AP_SCHEDULE:-0}  (disable ${AP_DISABLE_TIME:-02:00}, re-enable ${AP_ENABLE_TIME:-07:00})"
    echo "    • WiFi QR: cat /usr/local/share/travel-router/wifi-qr/wifi-qr.txt"
    echo "    • ntfy.sh: ${NTFY_TOPIC:-not configured (set NTFY_TOPIC in /etc/default/travel-router)}"
    echo "    • RaspAP: admin / ${RASPAP_PASS_DISPLAY:-<rotated — see above>}"
    echo ""
    echo "  Next steps:"
    [[ -z "${TS_KEY:-}" ]] && echo "    1. sudo tailscale up ${TAILSCALE_UP_ARGS:-}"
    echo "    2. sudo reboot  ← activates USB gadget mode (dwc2) + log2ram"
    echo "    3. Connect via USB-C → ssh root@192.168.7.1"
    echo "    4. Edit /etc/default/travel-router to set NTFY_TOPIC + IPHONE_BT_MAC"
    echo "    5. RaspAP web UI: http://${_AP_GATEWAY}  (admin / ${RASPAP_PASS_DISPLAY:-<rotated>})"
    echo ""
}
