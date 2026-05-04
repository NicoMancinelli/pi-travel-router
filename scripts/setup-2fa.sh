#!/bin/bash
# setup-2fa.sh — Configure SSH two-factor authentication (TOTP via Google Authenticator).
# Run once as the user who will SSH in: sudo -u <user> bash /usr/local/bin/setup-2fa.sh
# Requires: libpam-google-authenticator (installed by install.sh when ENABLE_2FA=1)

set -euo pipefail

if [[ $EUID -eq 0 && -z "${SUDO_USER:-}" ]]; then
    printf "Run as the SSH user, not root directly:\n"
    printf "  sudo -u <username> %s\n" "$0"
    exit 1
fi

TARGET_USER="${SUDO_USER:-$USER}"

if ! command -v google-authenticator >/dev/null 2>&1; then
    printf "google-authenticator not found. Install first:\n"
    printf "  sudo apt-get install -y libpam-google-authenticator\n"
    exit 1
fi

printf "Setting up TOTP 2FA for SSH login as '%s'.\n" "$TARGET_USER"
printf "You will need an authenticator app (Google Authenticator, Authy, etc.).\n\n"

# Run as the target user (may already be running as them via sudo -u)
google-authenticator \
    --time-based \
    --disallow-reuse \
    --force \
    --rate-limit=3 \
    --rate-time=30 \
    --window-size=3 \
    --quiet \
    --qr-mode=UTF8 \
    --no-confirm

printf "\n\nScan the QR code above with your authenticator app.\n"
printf "Emergency scratch codes are stored in ~/.google_authenticator\n\n"
printf "2FA is now active for SSH logins.\n"
