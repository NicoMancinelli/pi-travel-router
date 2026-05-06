#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║   pi-travel-router — SSH TOTP 2FA Setup                                 ║
# ║   Configures Google Authenticator TOTP for the root SSH account         ║
# ╚══════════════════════════════════════════════════════════════════════════╝
#
# Usage: sudo bash /usr/local/bin/setup-2fa.sh
# Run after install.sh with ENABLE_2FA=1. Generates a TOTP secret for root,
# displays the QR code, and optionally enforces 2FA by removing 'nullok'.

set -euo pipefail

# ── Colours & helpers ─────────────────────────────────────────────────────────
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; C='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${G}✓${NC} $*"; }
info() { echo -e "${C}→${NC} $*"; }
warn() { echo -e "${Y}⚠${NC} $*"; }
die()  { echo -e "${R}✗ FATAL:${NC} $*" >&2; exit 1; }

# ── 1. Must run as root ───────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

# ── 2. Source config and check ENABLE_2FA ─────────────────────────────────────
CONFIG_FILE="/etc/default/travel-router"
# shellcheck source=/dev/null
source "$CONFIG_FILE" 2>/dev/null || die "Config file not found: $CONFIG_FILE — has install.sh been run?"

if [[ "${ENABLE_2FA:-0}" != "1" ]]; then
    echo -e "${Y}2FA is not enabled in $CONFIG_FILE (ENABLE_2FA=${ENABLE_2FA:-0}).${NC}"
    echo "  Re-run install.sh with ENABLE_2FA=1, or set ENABLE_2FA=1 in $CONFIG_FILE and"
    echo "  ensure /etc/pam.d/sshd contains 'pam_google_authenticator.so'."
    exit 0
fi

# ── 3. Ensure google-authenticator is installed ───────────────────────────────
if ! command -v google-authenticator >/dev/null 2>&1; then
    info "google-authenticator not found — installing libpam-google-authenticator…"
    apt-get install -y libpam-google-authenticator
    ok "libpam-google-authenticator installed"
fi

# ── 4. Check for existing configuration ──────────────────────────────────────
GA_FILE="${HOME}/.google_authenticator"
if [[ -f "$GA_FILE" ]]; then
    warn "2FA is already configured (${GA_FILE} exists)."
    printf "  Reconfigure and generate a new TOTP secret? [y/N] "
    read -r _reconfigure
    if [[ ! "$_reconfigure" =~ ^[Yy]$ ]]; then
        echo "Keeping existing 2FA configuration. Exiting."
        exit 0
    fi
    info "Backing up existing config to ${GA_FILE}.bak"
    cp "$GA_FILE" "${GA_FILE}.bak"
fi

# ── 5. Header ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${C}━━ SSH Two-Factor Authentication Setup ━━${NC}"
echo ""
echo "  This script will:"
echo "    1. Generate a TOTP secret for the root account"
echo "    2. Display a QR code to scan with your authenticator app"
echo "    3. Optionally enforce 2FA by removing the 'nullok' fallback"
echo ""
echo "  You will need: Google Authenticator, Authy, or any TOTP-compatible app."
echo ""

# ── 6 & 7. Generate TOTP secret non-interactively ────────────────────────────
info "Generating TOTP secret…"
if ! google-authenticator \
        --time-based \
        --disallow-reuse \
        --force \
        --rate-limit=3 \
        --rate-time=30 \
        --window-size=3 \
        --no-confirm \
        --quiet 2>/dev/null; then
    # Some older versions don't support --no-confirm; fall back to short flags
    info "Retrying with short flags (older google-authenticator)…"
    google-authenticator -t -d -f -r 3 -R 30 -w 3 -q
fi

[[ -f "$GA_FILE" ]] || die "google-authenticator ran but ${GA_FILE} was not created."
ok "TOTP secret generated and stored in ${GA_FILE}"

# ── 8. Extract the secret key (first line of .google_authenticator) ───────────
TOTP_SECRET=""
TOTP_SECRET=$(head -n 1 "$GA_FILE")
[[ -n "$TOTP_SECRET" ]] || die "Could not read TOTP secret from ${GA_FILE}."

# ── 9. Display QR code or raw TOTP URI ───────────────────────────────────────
TOTP_ISSUER="TravelRouter"
TOTP_ACCOUNT="root@$(hostname)"
TOTP_URI="otpauth://totp/${TOTP_ISSUER}:${TOTP_ACCOUNT}?secret=${TOTP_SECRET}&issuer=${TOTP_ISSUER}"

echo ""
echo -e "${C}━━ Scan this QR code with your authenticator app ━━${NC}"
echo ""

if command -v qrencode >/dev/null 2>&1; then
    qrencode -t ansiutf8 "$TOTP_URI"
else
    warn "qrencode not installed — displaying raw TOTP URI instead."
    echo "  Install qrencode for a scannable QR code:  apt-get install -y qrencode"
    echo ""
    echo -e "  ${Y}TOTP URI (enter manually in your app):${NC}"
    echo "  $TOTP_URI"
fi

# ── 10. Instructions ──────────────────────────────────────────────────────────
echo ""
echo -e "${G}━━ Next steps ━━${NC}"
echo ""
echo "  1. Open Google Authenticator, Authy, or any TOTP app on your phone."
echo "  2. Tap 'Add account' → 'Scan QR code' and scan the code above."
echo "     (Or tap 'Enter setup key' and paste the URI if you can't scan.)"
echo ""
# M20: only print the raw secret when running interactively (stdout is a TTY)
[ -t 1 ] && echo -e "  ${Y}Secret key (for manual entry):${NC} ${TOTP_SECRET}"
echo ""

# ── 11 & 12. Verification guidance (no programmatic check) ───────────────────
echo -e "  ${C}Scratch codes${NC} (one-time emergency codes — store safely):"
# Print lines 2–6 of .google_authenticator (the emergency scratch codes)
scratch_codes=""
scratch_codes=$(tail -n +2 "$GA_FILE" | grep -E '^[0-9]{8}$' || true)
if [[ -n "$scratch_codes" ]]; then
    while IFS= read -r code; do
        echo "    $code"
    done <<< "$scratch_codes"
else
    echo "    (none found — check ${GA_FILE} manually)"
fi
echo ""

warn "2FA is now ACTIVE for the root account."
echo "  Test it: open a NEW SSH session and confirm you are prompted"
echo "  for a 6-digit code AFTER your password/key authentication."
echo "  Do NOT close this session until you have verified login works."
echo ""

# ── 13. Optionally remove 'nullok' to enforce 2FA ────────────────────────────
PAM_SSHD="/etc/pam.d/sshd"
if grep -q "nullok" "$PAM_SSHD" 2>/dev/null; then
    echo -e "${Y}━━ Enforce 2FA (remove nullok) ━━${NC}"
    echo ""
    echo "  Currently, users WITHOUT a ~/.google_authenticator file can still"
    echo "  log in (the 'nullok' option in ${PAM_SSHD})."
    echo "  Removing 'nullok' will REQUIRE 2FA for every login."
    echo ""
    warn "Only do this after you have verified that 2FA codes work in a new session."
    printf "  Remove nullok to enforce 2FA for all logins? [y/N] "
    read -r _enforce
    if [[ "$_enforce" =~ ^[Yy]$ ]]; then
        sed -i 's/ nullok//g; s/nullok //g; s/nullok//g' "$PAM_SSHD"
        ok "nullok removed from ${PAM_SSHD} — 2FA is now required for all logins."
        warn "IMPORTANT: Keep this SSH session open. Open a new session now to"
        warn "confirm 2FA works before closing your current connection."
    else
        info "nullok kept — users without ~/.google_authenticator can still log in."
    fi
else
    info "nullok not found in ${PAM_SSHD} — 2FA is already enforced (or PAM line differs)."
fi

# ── 14. Final summary ─────────────────────────────────────────────────────────
echo ""
echo -e "${G}━━ Summary ━━${NC}"
echo ""
[ -t 1 ] && ok "TOTP secret:     ${TOTP_SECRET}"
ok "Config file:     ${GA_FILE}"
ok "PAM config:      ${PAM_SSHD}"
if grep -q "nullok" "$PAM_SSHD" 2>/dev/null; then
    warn "2FA mode:        optional (nullok present — non-configured users bypass 2FA)"
else
    ok "2FA mode:        enforced (nullok absent — all logins require 2FA)"
fi
echo ""
ok "SSH 2FA setup complete."
echo ""
