#!/bin/bash
# setup-headscale.sh — Run on a public VPS to install Headscale.
# After running, copy the printed HEADSCALE_URL and TS_KEY to the Pi installer.
# Supports: Ubuntu/Debian amd64 and arm64. Requires curl, systemd.

set -euo pipefail

# Detect architecture
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)          HS_ARCH="amd64" ;;
    aarch64|arm64)   HS_ARCH="arm64" ;;
    *) printf "Unsupported architecture: %s\n" "$ARCH" >&2; exit 1 ;;
esac

# Resolve latest release tag
LATEST=$(curl -fsSL -o /dev/null -w '%{redirect_url}' \
    https://github.com/juanfont/headscale/releases/latest \
    | grep -o '[^/]*$' | tr -d 'v\r\n') || true
LATEST="${LATEST:-0.23.0}"

printf "==> Installing Headscale v%s (%s)...\n" "$LATEST" "$HS_ARCH"
curl -fsSL -o /usr/local/bin/headscale \
    "https://github.com/juanfont/headscale/releases/download/v${LATEST}/headscale_${LATEST}_linux_${HS_ARCH}"
chmod +x /usr/local/bin/headscale

mkdir -p /etc/headscale /var/lib/headscale

# Detect public IP
SERVER_IP=$(curl -fsSL https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

read -rp "Domain name for TLS/ACME (leave blank for plain HTTP on port 8080): " HS_DOMAIN
HS_DOMAIN="${HS_DOMAIN:-}"

if [[ -n "$HS_DOMAIN" ]]; then
    SERVER_URL="https://${HS_DOMAIN}"
    LISTEN_ADDR="0.0.0.0:443"
    TLS_CONFIG="
tls_letsencrypt_hostname: \"${HS_DOMAIN}\"
tls_letsencrypt_challenge_type: HTTP-01
tls_letsencrypt_listen: \":80\""
else
    SERVER_URL="http://${SERVER_IP}:8080"
    LISTEN_ADDR="0.0.0.0:8080"
    TLS_CONFIG=""
fi

cat > /etc/headscale/config.yaml << EOF
server_url: ${SERVER_URL}
listen_addr: ${LISTEN_ADDR}
grpc_listen_addr: 127.0.0.1:50443
grpc_allow_insecure: false
private_key_path: /var/lib/headscale/private.key
noise:
  private_key_path: /var/lib/headscale/noise_private.key
prefixes:
  v4: 100.64.0.0/10
  v6: fd7a:115c:a1e0::/48
derp:
  server:
    enabled: false
  urls:
    - https://controlplane.tailscale.com/derpmap/default
disable_check_updates: true
log:
  level: info
dns:
  base_domain: headscale.net
db_type: sqlite
db_path: /var/lib/headscale/db.sqlite
unix_socket: /var/run/headscale/headscale.sock
unix_socket_permission: "0770"
${TLS_CONFIG}
EOF

cat > /etc/systemd/system/headscale.service << 'UNIT'
[Unit]
Description=Headscale — self-hosted Tailscale control server
After=network-online.target

[Service]
Type=simple
User=headscale
ExecStart=/usr/local/bin/headscale serve
Restart=on-failure
RestartSec=10s
RuntimeDirectory=headscale

[Install]
WantedBy=multi-user.target
UNIT

id -u headscale 2>/dev/null || useradd -r -s /sbin/nologin headscale
mkdir -p /var/run/headscale /var/lib/headscale
chown -R headscale:headscale /var/run/headscale /var/lib/headscale /etc/headscale

systemctl daemon-reload
systemctl enable --now headscale

printf "==> Waiting for Headscale to start...\n"
sleep 4

headscale users create travel 2>/dev/null || true

PREAUTH=""
if command -v jq >/dev/null 2>&1; then
    PREAUTH=$(headscale preauthkeys create --user travel --expiration 24h --output json \
        | jq -r '.key // empty')
else
    PREAUTH=$(headscale preauthkeys create --user travel --expiration 24h --output json \
        | awk -F'"key":"' 'NF>1{split($2,a,"\""); print a[1]}')
fi

printf "\n================================================================\n"
printf "Headscale is running at: %s\n\n" "$SERVER_URL"
printf "Pass these to the Pi installer when prompted:\n\n"
printf "  Headscale URL:  %s\n" "$SERVER_URL"
printf "  Pre-auth key:   %s\n" "${PREAUTH:-<run: headscale preauthkeys create --user travel>}"
printf "\nManage your Tailnet: headscale nodes list\n"
printf "================================================================\n"
