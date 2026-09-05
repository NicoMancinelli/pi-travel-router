#!/bin/bash
# install/10-finalize.sh — version stamp, repo copy, and install summary
# Defines run_finalize(). Source this file; do not execute directly.

run_finalize() {
    # ── USB/SD storage mount manager ─────────────────────────────────────────────
    section "Storage mount manager"
    cp "${REPO}/scripts/mount-storage.sh" /usr/local/sbin/mount-storage.sh
    chmod +x /usr/local/sbin/mount-storage.sh
    mkdir -p /media/travel-data
    ok "mount-storage.sh installed to /usr/local/sbin/mount-storage.sh"
    ok "/media/travel-data created"

    # ── Read-only root (overlayfs) toggle ─────────────────────────────────────────
    section "Read-only root toggle (overlayfs)"
    cp "${REPO}/scripts/overlayfs-ctl.sh" /usr/local/sbin/overlayfs-ctl.sh
    chmod +x /usr/local/sbin/overlayfs-ctl.sh
    ok "overlayfs-ctl.sh installed — sudo overlayfs-ctl.sh enable|disable|status"

    # ── Speed test script + optional speedtest-cli ───────────────────────────────
    section "Speed test setup"
    cp "${REPO}/scripts/speedtest.sh" /usr/local/sbin/speedtest.sh
    chmod +x /usr/local/sbin/speedtest.sh
    # Best-effort: try to install speedtest-cli (Python). Failure is non-fatal.
    apt-get install -y python3-speedtest-cli 2>/dev/null \
        || pip3 install speedtest-cli 2>/dev/null \
        || true
    ok "speedtest.sh installed to /usr/local/sbin/speedtest.sh"

    # ── DoH resolver selector ────────────────────────────────────────────────────
    section "DoH resolver selector"
    cp "${REPO}/scripts/set-doh-resolver.sh" /usr/local/sbin/set-doh-resolver.sh
    chmod +x /usr/local/sbin/set-doh-resolver.sh
    ok "set-doh-resolver.sh installed to /usr/local/sbin/set-doh-resolver.sh"

    # ── Privacy profiles ─────────────────────────────────────────────────────────
    section "Privacy profiles"
    mkdir -p /etc/travel-router/privacy-profiles
    for _yaml in "${REPO}/config/privacy-profiles/"*.yaml; do
        cp "${_yaml}" /etc/travel-router/privacy-profiles/
    done
    cp "${REPO}/scripts/apply-privacy-profile.sh" /usr/local/sbin/apply-privacy-profile.sh
    chmod +x /usr/local/sbin/apply-privacy-profile.sh
    cp "${REPO}/scripts/config-backup.sh" /usr/local/sbin/config-backup.sh
    chmod +x /usr/local/sbin/config-backup.sh
    mkdir -p /var/lib/travel-router
    echo "vpn-only" > /var/lib/travel-router/active-profile
    ok "Privacy profiles installed (default: vpn-only)"

    # ── Captive portal check ─────────────────────────────────────────────────────
    section "Captive portal check"
    cp "${REPO}/scripts/captive-check.sh" /usr/local/sbin/captive-check.sh
    chmod +x /usr/local/sbin/captive-check.sh
    mkdir -p /var/lib/travel-router
    if [[ ! -f /var/lib/travel-router/captive-portal.json ]]; then
        echo '{"detected":false}' > /var/lib/travel-router/captive-portal.json
    fi
    touch /var/lib/travel-router/captive-creds.json 2>/dev/null || true
    ok "captive-check.sh installed; captive-portal.json initialised"

    # ── Bandwidth history store ───────────────────────────────────────────────────
    section "Bandwidth history"
    mkdir -p /var/lib/travel-router && touch /var/lib/travel-router/bw-history.json || true
    ok "Bandwidth history store initialised"

    # ── Wake-on-LAN targets store ─────────────────────────────────────────────────
    section "Wake-on-LAN"
    touch /var/lib/travel-router/wol-targets.json 2>/dev/null || true
    ok "Wake-on-LAN targets store initialised"

    # ── Device alias store ────────────────────────────────────────────────────────
    section "Device alias store"
    touch /var/lib/travel-router/aliases.json 2>/dev/null || true
    ok "Device alias store initialised"

    # ── Port forwarding store ─────────────────────────────────────────────────────
    section "Port forwarding store"
    touch /var/lib/travel-router/portforward.json 2>/dev/null || true
    ok "Port forwarding store initialised"

    # ── DNS hosts override store ──────────────────────────────────────────────────
    section "DNS hosts override store"
    touch /var/lib/travel-router/dns-hosts.json 2>/dev/null || true
    touch /etc/hosts.travel-router 2>/dev/null || true
    ok "DNS hosts override store initialised"

    # ── Static DHCP reservations store ────────────────────────────────────────────
    section "Static DHCP reservations store"
    touch /var/lib/travel-router/dhcp-reservations.json 2>/dev/null || true
    ok "Static DHCP reservations store initialised"

    # ── Data cap store ────────────────────────────────────────────────────────────
    section "Data cap store"
    touch /var/lib/travel-router/datacap.json 2>/dev/null || true
    ok "Data cap store initialised"

    # ── Client history store ──────────────────────────────────────────────────────
    section "Client history store"
    touch /var/lib/travel-router/client-history.json 2>/dev/null || true
    ok "Client history store initialised"

    # ── Ping monitor store ────────────────────────────────────────────────────────
    section "Ping monitor store"
    touch /var/lib/travel-router/ping-hosts.json 2>/dev/null || true
    ok "Ping monitor store initialised"

    # ── Speedtest history store ────────────────────────────────────────────────────
    section "Speedtest history store"
    touch /var/lib/travel-router/speedtest-history.json 2>/dev/null || true
    ok "Speedtest history store initialised"

    # ── Uplink history store ───────────────────────────────────────────────────────
    section "Uplink history store"
    touch /var/lib/travel-router/uplink-history.json 2>/dev/null || true
    ok "Uplink history store initialised"

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
    echo "    • Web management dashboard (http://${_AP_GATEWAY}:8080 after boot)"
    echo "    • AP SSID: ${AP_SSID:-TravelRouter}  (on uap0, ${_AP_SUBNET})"
    echo "    • USB gadget: usb0 → 192.168.7.1  (active after reboot)"
    echo "    • iPhone USB tether: udev auto-detect (enx*, metric 100)"
    echo "    • Android USB tether: udev auto-detect (rndis0/usb0, metric 200)"
    echo "    • Bluetooth tether: set IPHONE_BT_MAC in /etc/default/travel-router"
    echo "    • Uplink failover watchdog: 30s timer"
    echo "    • WAN watchdog + captive portal detection: 60s timer"
    echo "    • TTL=65 + DSCP strip (Visible carrier bypass)"
    echo "    • DNS-over-TLS: ${ENABLE_DOT:-0}  (stubby → Cloudflare/Quad9)"
    echo "    • VPN kill switch: ${ENABLE_VPN_KILLSWITCH:-0}  (AP traffic blocked if Tailscale drops)"
    echo "    • Auto security updates: ${ENABLE_AUTO_UPDATES:-0}  (unattended-upgrades, reboot 03:30)"
    echo "    • Auto-update: weekly check (Sun 03:00) — run manually: sudo update-router.sh"
    echo "    • Per-device VPN: ${ENABLE_PER_DEVICE_VPN:-0}  (set VPN_DEVICE_MACS in /etc/default/travel-router)"
    echo "    • WAN metric management: ${ENABLE_WAN_METRICS:-1}  (enx*=100 rndis0=200 bnep0=300 wlan0=600)"
    echo "    • USB file sharing (travel NAS): ${ENABLE_USB_SHARE:-0}  (smb://${_AP_GATEWAY}/${USB_SHARE_NAME:-TravelData})"
    echo "    • Read-only root toggle: sudo overlayfs-ctl.sh enable|disable|status"
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
    echo "    • WiFi QR: cat /usr/local/share/travel-router/wifi-qr/wifi-qr.txt"
    echo "    • ntfy.sh: ${NTFY_TOPIC:-not configured (set NTFY_TOPIC in /etc/default/travel-router)}"
    local _WEB_TOKEN
    _WEB_TOKEN=$(cat /var/lib/travel-router/web-token 2>/dev/null || echo "not yet generated")
    echo "    • Web dashboard: http://${_AP_GATEWAY}:8080  (token: ${_WEB_TOKEN})"
    echo ""
    echo "  Next steps:"
    [[ -z "${TS_KEY:-}" ]] && echo "    1. sudo tailscale up ${TAILSCALE_UP_ARGS:-}"
    echo "    2. sudo reboot  ← activates USB gadget mode (dwc2) + log2ram"
    echo "    3. Connect via USB-C → ssh root@192.168.7.1"
    echo "    4. Edit /etc/default/travel-router to set NTFY_TOPIC + IPHONE_BT_MAC"
    echo "    5. Web dashboard: http://${_AP_GATEWAY}:8080  (token: cat /var/lib/travel-router/web-token)"
    echo ""
}
