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

    install_file scripts/tailscale-exit-node.sh /usr/local/bin/tailscale-exit-node.sh 755
    ok "Tailscale exit node manager installed (/usr/local/bin/tailscale-exit-node.sh)"

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
tmpl, dest, privkey, addr, port, network = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
with open(tmpl) as f: content = f.read()
content = content.replace('__WG_PRIVATE_KEY__', privkey)
content = content.replace('__WG_SERVER_ADDRESS__', addr)
content = content.replace('__WG_LISTEN_PORT__', port)
content = content.replace('__WG_NETWORK__', network)
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
          "${WG_LISTEN_PORT:-51820}" \
          "${WG_NETWORK:-10.9.0.0/24}"

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
}
