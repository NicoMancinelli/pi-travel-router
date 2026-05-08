#!/bin/bash
# install/09-services.sh — install and enable all systemd units + scripts
# Defines run_services(). Source this file; do not execute directly.

run_services() {
    section "Scripts → /usr/local/bin/"

    # Scripts not covered by other modules
    for script in \
        vnstat-metrics.sh update-blocklists.sh travel-router-firewall.sh \
        ap-schedule.sh \
        update-router.sh \
        travel-status.sh; do
        install_file "scripts/$script" "/usr/local/bin/$script" 755
        ok "  $script"
    done

    install_file scripts/travel-diagnostic.sh /usr/local/bin/travel-diagnostic 755

    # TUI: Python (preferred) + bash fallback
    cp "${REPO}/scripts/travel-tui.py" /usr/local/sbin/travel-tui.py
    chmod 0755 /usr/local/sbin/travel-tui.py
    ok "  travel-tui.py → /usr/local/sbin/travel-tui.py"

    cp "${REPO}/scripts/travel-tui-legacy.sh" /usr/local/sbin/travel-tui-legacy
    chmod 0755 /usr/local/sbin/travel-tui-legacy
    ok "  travel-tui-legacy.sh → /usr/local/sbin/travel-tui-legacy"

    cat > /usr/local/sbin/travel-tui << 'EOF'
#!/bin/bash
if python3 -c "import textual" 2>/dev/null; then
    exec python3 /usr/local/sbin/travel-tui.py "$@"
else
    exec /usr/local/sbin/travel-tui-legacy "$@"
fi
EOF
    chmod 0755 /usr/local/sbin/travel-tui
    ok "  travel-tui wrapper → /usr/local/sbin/travel-tui"

    # Captive portal hooks directory
    mkdir -p /etc/travel-router/portals/examples
    chmod 0750 /etc/travel-router/portals
    chmod 0750 /etc/travel-router/portals/examples
    if [[ -d "${REPO}/scripts/portals" ]]; then
        for f in "${REPO}"/scripts/portals/*.sh; do
            [[ -f "$f" ]] || continue
            cp "$f" /etc/travel-router/portals/examples/
            chmod 0640 /etc/travel-router/portals/examples/"$(basename "$f")"
        done
        ok "Portal example scripts installed to /etc/travel-router/portals/examples/"
    fi

    # Pairing docs
    mkdir -p /usr/local/share/travel-router-docs
    cat > /usr/local/share/travel-router-docs/bluetooth-pair.txt << 'EOF'
# iPhone Bluetooth Tethering — One-Time Pairing
#
# 1. iPhone: Settings → Bluetooth → Enable (leave screen open)
# 2. On Pi:
#    sudo bluetoothctl
#    power on
#    agent on
#    scan on
#    pair XX:XX:XX:XX:XX:XX
#    trust XX:XX:XX:XX:XX:XX
#    quit
# 3. Set IPHONE_BT_MAC="XX:XX:XX:XX:XX:XX" in /etc/default/travel-router
# 4. Connect: sudo /usr/local/bin/start-bt-tether.sh
EOF

    # MOTD
    section "MOTD + status command"
    install_file config/motd-travel-router /etc/update-motd.d/10-travel-router 755
    chmod +x /etc/update-motd.d/10-travel-router
    ok "SSH login MOTD installed (calls travel-status.sh)"

    # ── /etc/default/travel-router ──────────────────────────────────────────────
    section "Travel router config defaults"

    install_file config/travel-router-defaults /etc/default/travel-router 600

    local _DEFAULTS_FILE="/etc/default/travel-router"
    local _ENABLE_WAN_METRICS="${ENABLE_WAN_METRICS:-1}"
    local _AP_SUBNET="${AP_SUBNET:-10.3.141.0/24}"
    local _AP_GATEWAY="${AP_GATEWAY:-10.3.141.1}"

    for flag in ENABLE_OPEN_WIFI_FALLBACK ENABLE_HTTP_UA_REWRITE ENABLE_TOR_TRANSPARENT \
        ENABLE_BLOCKLISTS ENABLE_DOT ENABLE_VPN_KILLSWITCH ENABLE_AUTO_UPDATES \
        ENABLE_AVAHI_REFLECTOR ENABLE_ADGUARD ENABLE_AP_SCHEDULE ENABLE_CLIENT_QOS \
        ENABLE_PER_DEVICE_VPN ENABLE_CAKE_AUTOTUNE ENABLE_SPLIT_TUNNEL ENABLE_2FA \
        ENABLE_WAN_METRICS ENABLE_BANDWIDTH_DASHBOARD ENABLE_PROMETHEUS_EXPORTER \
        ENABLE_UPS_MONITOR ENABLE_WIREGUARD; do
        _safe_write_conf "$flag" "${!flag:-0}" "$_DEFAULTS_FILE"
    done

    _safe_write_conf "NTFY_TOPIC"            "${NTFY_TOPIC:-}"               "$_DEFAULTS_FILE"
    _safe_write_conf "HEADSCALE_URL"         "${HEADSCALE_URL:-}"            "$_DEFAULTS_FILE"
    _safe_write_conf "SPLIT_TUNNEL_DOMAINS"  "${SPLIT_TUNNEL_DOMAINS:-}"     "$_DEFAULTS_FILE"
    _safe_write_conf "IPHONE_BT_MAC"         "${IPHONE_BT_MAC:-}"            "$_DEFAULTS_FILE"
    _safe_write_conf "SSH_ADMIN_KEY"         "${SSH_ADMIN_KEY:-}"            "$_DEFAULTS_FILE"
    _safe_write_conf "AP_CLIENT_BANDWIDTH"   "${AP_CLIENT_BANDWIDTH:-unlimited}" "$_DEFAULTS_FILE"
    _safe_write_conf "AP_DISABLE_TIME"       "${AP_DISABLE_TIME:-02:00}"     "$_DEFAULTS_FILE"
    _safe_write_conf "AP_ENABLE_TIME"        "${AP_ENABLE_TIME:-07:00}"      "$_DEFAULTS_FILE"
    _safe_write_conf "VPN_DEVICE_MACS"       "${VPN_DEVICE_MACS:-}"          "$_DEFAULTS_FILE"
    _safe_write_conf "TOR_AP_PASS"           ""                              "$_DEFAULTS_FILE" 2>/dev/null || true
    _safe_write_conf "PUSHGW_URL"              "${PUSHGW_URL:-}"                    "$_DEFAULTS_FILE"
    _safe_write_conf "UPS_SHUTDOWN_THRESHOLD"  "${UPS_SHUTDOWN_THRESHOLD:-10}"      "$_DEFAULTS_FILE"
    _safe_write_conf "TAILSCALE_UP_ARGS"       "${TAILSCALE_UP_ARGS:-}"             "$_DEFAULTS_FILE"
    _safe_write_conf "WG_LISTEN_PORT"          "${WG_LISTEN_PORT:-51820}"           "$_DEFAULTS_FILE"
    _safe_write_conf "WG_PEER_PUBKEY"          "${WG_PEER_PUBKEY:-}"                "$_DEFAULTS_FILE"
    _safe_write_conf "WG_PEER_ENDPOINT"        "${WG_PEER_ENDPOINT:-}"              "$_DEFAULTS_FILE"
    _safe_write_conf "WG_PEER_ALLOWED_IPS"     "${WG_PEER_ALLOWED_IPS:-}"           "$_DEFAULTS_FILE"

    ok "/etc/default/travel-router written"

    # ── Systemd units ────────────────────────────────────────────────────────────
    section "Systemd units"

    local _SYSTEMD_DEST="/etc/systemd/system"
    for unit in \
        failover-watchdog.service failover-watchdog.timer \
        tether@.service \
        wan-watchdog.service wan-watchdog.timer \
        cpu-performance.service cake-qdisc.service \
        wlan-mac-random.service \
        vnstat-metrics.service vnstat-metrics.timer \
        update-blocklists.service update-blocklists.timer \
        tailscale-watchdog.service tailscale-watchdog.timer \
        wireguard-watchdog.service wireguard-watchdog.timer \
        adguard-home.service \
        ap-disable.service ap-disable.timer \
        ap-enable.service ap-enable.timer \
        daily-digest.service daily-digest.timer \
        update-router.service update-router.timer \
        tune-cake.service tune-cake.timer; do
        install_file "systemd/$unit" "$_SYSTEMD_DEST/$unit" 644
        ok "  $unit"
    done

    systemctl daemon-reload

    for unit in \
        failover-watchdog.timer wan-watchdog.timer \
        cpu-performance.service cake-qdisc.service \
        wlan-mac-random.service \
        vnstat-metrics.timer update-blocklists.timer \
        tailscale-watchdog.timer \
        wireguard-watchdog.timer \
        daily-digest.timer \
        update-router.timer; do
        if run_or_dry systemctl enable "$unit" 2>/dev/null; then
            ok "  enabled: $unit"
        else
            warn "  could not enable $unit"
        fi
    done

    # Initial blocklist load
    if [[ "${ENABLE_BLOCKLISTS:-0}" = "1" ]]; then
        info "Running initial blocklist load (this may take ~30s)..."
        if run_or_dry systemctl start update-blocklists.service 2>/dev/null; then
            ok "Initial blocklist loaded"
        else
            warn "Initial blocklist load failed — will retry at next timer fire"
        fi
    fi

    # Web dashboard
    section "Web management dashboard"
    local _WEB_TOKEN_FILE="/var/lib/travel-router/web-token"
    mkdir -p /var/lib/travel-router
    if [[ ! -f "$_WEB_TOKEN_FILE" ]]; then
        python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$_WEB_TOKEN_FILE"
        chmod 0600 "$_WEB_TOKEN_FILE"
    fi
    cp -r "${REPO}/web" /opt/pi-travel-router/ 2>/dev/null || true
    install_file systemd/travel-router-web.service /etc/systemd/system/travel-router-web.service 644
    systemctl daemon-reload
    run_or_dry systemctl enable travel-router-web.service 2>/dev/null || true
    ok "Web dashboard enabled on :8080 — token at $_WEB_TOKEN_FILE"

    # WiFi QR code
    section "WiFi QR code"
    local _WIFI_QR_DIR="/usr/local/share/travel-router/wifi-qr"
    mkdir -p "$_WIFI_QR_DIR"
    if [[ -n "${AP_SSID:-}" && -n "${AP_PASS:-}" ]]; then
        if [[ "${AP_SSID}" =~ ['`$()\\'] ]] || [[ "${AP_PASS}" =~ ['`$()\\'] ]]; then
            warn "AP_SSID or AP_PASS contains shell-unsafe characters; skipping QR code generation"
        else
            local _WIFI_STRING
            _WIFI_STRING=$(python3 -c "
import sys
ssid, pwd = sys.argv[1], sys.argv[2]
def mecard_escape(s):
    result = ''
    for c in s:
        if c in '\\\\;,\":':
            result += '\\\\' + c
        else:
            result += c
    return result
print('WIFI:T:WPA;S:' + mecard_escape(ssid) + ';P:' + mecard_escape(pwd) + ';;', end='')
" "$AP_SSID" "$AP_PASS")
            printf '%s\n' "$_WIFI_STRING" > "$_WIFI_QR_DIR/wifi-string.txt"
            chmod 600 "$_WIFI_QR_DIR/wifi-string.txt"
            if command -v qrencode >/dev/null 2>&1; then
                qrencode -t UTF8 -o "$_WIFI_QR_DIR/wifi-qr.txt" "$_WIFI_STRING"
                chmod 600 "$_WIFI_QR_DIR/wifi-qr.txt"
                ok "WiFi QR code saved to $_WIFI_QR_DIR/wifi-qr.txt"
                qrencode -t UTF8 "$_WIFI_STRING" > /dev/tty || true
            else
                ok "qrencode not available — WiFi string saved to $_WIFI_QR_DIR/wifi-string.txt"
            fi
        fi
    fi
}
