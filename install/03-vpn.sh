#!/bin/bash
# install/03-vpn.sh — Tailscale + WireGuard VPN setup
# Defines run_vpn(). Source this file; do not execute directly.

run_vpn() {
    section "Tailscale"

    run_or_dry systemctl enable --now tailscaled 2>/dev/null || true

    local _TS_KEY="${TS_KEY:-}"
    if [[ -n "$_TS_KEY" ]]; then
        # Validate TAILSCALE_UP_ARGS against forbidden flags
        read -ra _TS_ARGS <<< "${TAILSCALE_UP_ARGS:-}"
        local _FORBIDDEN_TS=("--authkey" "--reset" "--force-reauth" "--auth-key")
        for _targ in "${_TS_ARGS[@]}"; do
            for _f in "${_FORBIDDEN_TS[@]}"; do
                [[ "$_targ" = "$_f" || "$_targ" = "${_f}="* ]] && \
                    die "TAILSCALE_UP_ARGS contains forbidden flag: $_targ"
            done
        done
        local _TS_LOGIN_ARGS=()
        [[ -n "${HEADSCALE_URL:-}" ]] && _TS_LOGIN_ARGS+=(--login-server="${HEADSCALE_URL}")
        if run_or_dry tailscale up \
            --authkey="$_TS_KEY" \
            "${_TS_LOGIN_ARGS[@]}" \
            "${_TS_ARGS[@]}" \
            2>/dev/null; then
            ok "Tailscale authenticated"
        else
            warn "Tailscale auth failed — run manually: sudo tailscale up ${TAILSCALE_UP_ARGS:-}"
        fi
    else
        warn "No Tailscale key provided. After reboot, run:"
        if [[ -n "${HEADSCALE_URL:-}" ]]; then
            warn "  sudo tailscale up --login-server=\"${HEADSCALE_URL}\" ${TAILSCALE_UP_ARGS:-}"
        else
            warn "  sudo tailscale up ${TAILSCALE_UP_ARGS:-}"
        fi
    fi

    # ── WireGuard ───────────────────────────────────────────────────────────────
    section "WireGuard"

    local _ENABLE_WIREGUARD="${ENABLE_WIREGUARD:-0}"
    if [[ "$_ENABLE_WIREGUARD" = "1" ]]; then
        mkdir -p /etc/wireguard
        chmod 700 /etc/wireguard
        if [[ ! -f /etc/wireguard/wg0.key ]]; then
            wg genkey | tee /etc/wireguard/wg0.key | wg pubkey > /etc/wireguard/wg0.pub
            chmod 600 /etc/wireguard/wg0.key
        fi
        local _wg_server_addr
        _wg_server_addr=$(python3 -c "
import ipaddress, sys
n = ipaddress.ip_network(sys.argv[1], strict=False)
print(str(list(n.hosts())[0]))
" "${WG_NETWORK:-10.9.0.0/24}")
        python3 -c "
import sys, os, tempfile
tmpl, dest, privkey, addr, port = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
with open(tmpl) as f: content = f.read()
content = content.replace('__WG_PRIVATE_KEY__', privkey)
content = content.replace('__WG_SERVER_ADDRESS__', addr)
content = content.replace('__WG_LISTEN_PORT__', port)
fd, tmp = tempfile.mkstemp(dir='/etc/wireguard')
try:
    with os.fdopen(fd, 'w') as fh: fh.write(content)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
except:
    os.unlink(tmp); raise
" "${REPO}/config/wg0.conf.template" /etc/wireguard/wg0.conf \
          "$(cat /etc/wireguard/wg0.key)" \
          "$_wg_server_addr" \
          "${WG_LISTEN_PORT:-51820}"

        if [[ -n "${WG_PEER_PUBKEY:-}" ]]; then
            python3 -c "
import sys
path = '/etc/wireguard/wg0.conf'
peer_block = '\n[Peer]\nPublicKey = ' + sys.argv[1]
if sys.argv[2]: peer_block += '\nEndpoint = ' + sys.argv[2]
if sys.argv[3]: peer_block += '\nAllowedIPs = ' + sys.argv[3]
peer_block += '\n'
with open(path, 'a') as f: f.write(peer_block)
" "$WG_PEER_PUBKEY" "${WG_PEER_ENDPOINT:-}" "${WG_PEER_ALLOWED_IPS:-0.0.0.0/0}"
        fi

        run_or_dry systemctl enable wg-quick@wg0 2>/dev/null || true
        ok "WireGuard configured (wg0); public key: $(cat /etc/wireguard/wg0.pub 2>/dev/null || echo 'unknown')"
    else
        systemctl disable wg-quick@wg0 2>/dev/null || true
        ok "WireGuard disabled (set ENABLE_WIREGUARD=1 to activate)"
    fi

    # ── Tor transparent proxy ───────────────────────────────────────────────────
    section "Tor — optional transparent proxy config"

    if [[ "${ENABLE_TOR_TRANSPARENT:-0}" = "1" ]] && ! grep -q "TransPort 9040" /etc/tor/torrc 2>/dev/null; then
        cat >> /etc/tor/torrc << 'EOF'

# Transparent proxy (for Tor subnet 172.16.100.0/24)
VirtualAddrNetworkIPv4 10.192.0.0/10
AutomapHostsOnResolve 1
TransPort 9040 IsolateClientAddr
DNSPort 5353
EOF
        ok "Tor transparent proxy config added"
    elif [[ "${ENABLE_TOR_TRANSPARENT:-0}" = "1" ]]; then
        ok "Tor already configured for transparent proxy"
    fi

    if [[ "${ENABLE_TOR_TRANSPARENT:-0}" = "1" ]]; then
        run_or_dry systemctl enable tor 2>/dev/null || true
        ok "Tor enabled"

        if iw dev wlan0 interface add uap1 type __ap 2>/dev/null; then
            iw dev uap1 del 2>/dev/null || true
            [[ -n "${TOR_AP_PASS:-}" ]] || die "TOR_AP_PASS is empty"
            if ! grep -q "^bss=uap1" /etc/hostapd/hostapd.conf; then
                cat >> /etc/hostapd/hostapd.conf << 'TOREOF'

# Tor transparent-proxy AP
bss=uap1
ssid=TorAP
wpa=2
wpa_passphrase=PLACEHOLDER_TOR_PASS
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
TOREOF
                python3 -c "
import sys, os, tempfile
path = '/etc/hostapd/hostapd.conf'
with open(path) as f: lines = f.readlines()
out = []
in_tor_bss = False
for l in lines:
    if l.strip() == 'bss=uap1': in_tor_bss = True
    if in_tor_bss and l.startswith('wpa_passphrase=PLACEHOLDER_TOR_PASS'):
        out.append('wpa_passphrase=' + sys.argv[1] + '\n')
    else: out.append(l)
fd, tmp = tempfile.mkstemp(dir='/etc/hostapd', prefix='hostapd.conf.')
try:
    with os.fdopen(fd, 'w') as fh: fh.writelines(out)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except:
    os.unlink(tmp); raise
" "${TOR_AP_PASS}"
            fi

            install_file config/dnsmasq-tor-ap.conf /etc/dnsmasq.d/tor-ap.conf

            mkdir -p /etc/rc.local.d
            cat > /etc/rc.local.d/50-tor-uap1.sh << 'RCEOF'
#!/bin/sh
# Created by pi-travel-router — I-H7
iw dev wlan0 interface add uap1 type __ap || true
RCEOF
            chmod 755 /etc/rc.local.d/50-tor-uap1.sh

            if [[ -f /etc/rc.local ]] && ! grep -q "rc.local.d" /etc/rc.local; then
                sed -i '/^exit 0/i # Source rc.local drop-ins\nfor _f in /etc/rc.local.d/*.sh; do [ -f "$_f" ] \&\& . "$_f"; done' /etc/rc.local
            fi

            ok "uap1 supported — TorAP SSID configured"
        else
            warn "uap1 not supported by brcmfmac — Tor AP uses static-IP fallback"
        fi
    else
        systemctl disable --now tor 2>/dev/null || true
        rm -f /etc/rc.local.d/50-tor-uap1.sh
        ok "Tor disabled by default"
    fi
}
