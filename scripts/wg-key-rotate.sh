#!/bin/bash
# scripts/wg-key-rotate.sh — Rotate WireGuard private key and restart wg0
# Deployed to /usr/local/sbin/wg-key-rotate.sh by install/08-security.sh

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

WG_PRIVATE_KEY="/etc/wireguard/private.key"
WG_PUBLIC_KEY="/etc/wireguard/public.key"
WG_CONF="/etc/wireguard/wg0.conf"
NOTIFY="/usr/local/sbin/notify-router.sh"

log() { logger -t wg-key-rotate "$*"; echo "$*"; }

# Generate new keypair
log "Generating new WireGuard keypair"
wg genkey | tee "$WG_PRIVATE_KEY" | wg pubkey > "$WG_PUBLIC_KEY"
chmod 600 "$WG_PRIVATE_KEY"

# Read new private key
NEW_PRIVATE_KEY="$(cat "$WG_PRIVATE_KEY")"
NEW_PUBLIC_KEY="$(cat "$WG_PUBLIC_KEY")"

# Update PrivateKey line in wg0.conf, preserving all other config
if [[ -f "$WG_CONF" ]]; then
    # Use a temp file for atomic replacement
    tmp="$(mktemp --tmpdir wg0-conf.XXXXXXXX)"
    sed "s|^PrivateKey *= *.*|PrivateKey = ${NEW_PRIVATE_KEY}|" "$WG_CONF" > "$tmp"
    mv "$tmp" "$WG_CONF"
    chmod 600 "$WG_CONF"
    log "Updated PrivateKey in $WG_CONF"
else
    log "Warning: $WG_CONF not found — PrivateKey not updated"
fi

# Restart wg-quick@wg0 if the interface is active
if systemctl is-active --quiet "wg-quick@wg0" 2>/dev/null; then
    log "Restarting wg-quick@wg0"
    systemctl restart "wg-quick@wg0"
    log "wg-quick@wg0 restarted"
else
    log "wg-quick@wg0 not active — skipping restart"
fi

# Send notification if notify-router.sh is present
if [[ -x "$NOTIFY" ]]; then
    "$NOTIFY" "WireGuard key rotated — update peers with new public key: ${NEW_PUBLIC_KEY}"
fi

log "WireGuard key rotation complete. New public key: ${NEW_PUBLIC_KEY}"
exit 0
