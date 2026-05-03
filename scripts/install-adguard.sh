#!/bin/bash
# Downloads and installs AdGuard Home binary for the Pi's architecture.
# Called by install.sh when ENABLE_ADGUARD=1.

set -euo pipefail

AGH_DIR="/opt/AdGuardHome"
API_URL="https://api.github.com/repos/AdguardTeam/AdGuardHome/releases/latest"

ARCH=$(uname -m)
case "$ARCH" in
    armv7l)  AGH_ARCH="arm" ;;
    aarch64) AGH_ARCH="arm64" ;;
    *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

AGH_VERSION=$(curl -fsSL "$API_URL" | grep '"tag_name"' | cut -d'"' -f4)
TARBALL="AdGuardHome_linux_${AGH_ARCH}.tar.gz"
URL="https://github.com/AdguardTeam/AdGuardHome/releases/download/${AGH_VERSION}/${TARBALL}"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

curl -fsSL "$URL" -o "$TMP/$TARBALL"
tar -xzf "$TMP/$TARBALL" -C "$TMP"
mkdir -p "$AGH_DIR"
cp "$TMP/AdGuardHome/AdGuardHome" "$AGH_DIR/AdGuardHome"
chmod 755 "$AGH_DIR/AdGuardHome"
echo "AdGuard Home $AGH_VERSION installed to $AGH_DIR"
