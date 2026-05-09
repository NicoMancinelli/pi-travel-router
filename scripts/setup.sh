#!/usr/bin/env bash
# setup.sh — Bootstrap pi-travel-router on a fresh Raspberry Pi OS.
#
# One-liner usage (run directly on the Pi):
#   curl -fsSL https://raw.githubusercontent.com/NicoMancinelli/pi-travel-router/main/scripts/setup.sh | sudo bash
#
# Non-interactive (scripted) usage:
#   curl -fsSL .../setup.sh | sudo AP_PASS=mypassword INSTALL_NONINTERACTIVE=1 bash
#
# What this does:
#   1. Verifies it's running on a Raspberry Pi (aarch64/armv7l)
#   2. Installs git if not present
#   3. Clones the repo to /opt/pi-travel-router (or uses existing clone)
#   4. Hands off to install.sh from the cloned directory

set -euo pipefail

REPO_URL="https://github.com/NicoMancinelli/pi-travel-router.git"
INSTALL_DIR="/opt/pi-travel-router"

RED='\033[1;31m'; GRN='\033[1;32m'; YEL='\033[1;33m'; CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'

ok()      { printf "  ${GRN}✓${RST} %s\n" "$*"; }
info()    { printf "  ${CYN}→${RST} %s\n" "$*"; }
warn()    { printf "  ${YEL}!${RST} %s\n" "$*"; }
die()     { printf "\n  ${RED}✗ FATAL:${RST} %s\n\n" "$*" >&2; exit 1; }
section() { printf "\n${BLD}── %s ──${RST}\n" "$*"; }

printf "\n"
printf "${BLD}╔══════════════════════════════════════════════════╗${RST}\n"
printf "${BLD}║   pi-travel-router — Setup                       ║${RST}\n"
printf "${BLD}║   https://github.com/NicoMancinelli/pi-travel-router ║${RST}\n"
printf "${BLD}╚══════════════════════════════════════════════════╝${RST}\n\n"

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run as root:  curl ... | sudo bash"

# ── Architecture check ────────────────────────────────────────────────────────
ARCH="$(uname -m)"
case "${ARCH}" in
    aarch64|armv7l) ok "Architecture: ${ARCH}" ;;
    *) warn "Expected aarch64/armv7l — got ${ARCH}. Continuing, but this is only tested on Raspberry Pi." ;;
esac

# Check for Raspberry Pi hardware (best-effort)
if [[ -f /proc/device-tree/model ]]; then
    MODEL="$(tr -d '\0' < /proc/device-tree/model)"
    ok "Device: ${MODEL}"
else
    warn "Could not detect Pi model — /proc/device-tree/model not found"
fi

# ── OS check ──────────────────────────────────────────────────────────────────
# Accept any Debian/Raspberry Pi OS release (bullseye, bookworm, trixie, …)
OS_ID="$(grep '^ID=' /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"' || true)"
OS_CODENAME="$(grep '^VERSION_CODENAME=' /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"' || true)"
OS_PRETTY="$(grep '^PRETTY_NAME=' /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"' || true)"

case "${OS_ID}" in
    debian|raspbian)
        ok "OS: ${OS_PRETTY:-Debian/Raspberry Pi OS} (${OS_CODENAME:-unknown release})"
        ;;
    *)
        warn "Unrecognised OS (${OS_PRETTY:-unknown}). Expected Raspberry Pi OS or Debian — continuing anyway."
        ;;
esac

# ── Install git ───────────────────────────────────────────────────────────────
section "Dependencies"

if command -v git &>/dev/null; then
    ok "git already installed ($(git --version))"
else
    info "Installing git..."
    apt-get update -qq
    apt-get install -y -qq git
    ok "git installed"
fi

# ── Clone or update repo ──────────────────────────────────────────────────────
section "Fetching pi-travel-router"

if [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "Repo already present at ${INSTALL_DIR} — pulling latest..."
    git -C "${INSTALL_DIR}" pull --ff-only \
        || warn "Pull failed (maybe offline or detached HEAD) — using existing code"
    ok "Repo up to date"
else
    info "Cloning to ${INSTALL_DIR}..."
    git clone --depth=1 "${REPO_URL}" "${INSTALL_DIR}" \
        || die "Clone failed. Check your internet connection and try again."
    ok "Cloned to ${INSTALL_DIR}"
fi

# ── Hand off to install.sh ────────────────────────────────────────────────────
section "Starting installer"
printf "\n"

INSTALL_SCRIPT="${INSTALL_DIR}/install.sh"
[[ -f "${INSTALL_SCRIPT}" ]] || die "install.sh not found at ${INSTALL_SCRIPT}. Clone may be incomplete."

# When invoked via `curl | sudo bash`, stdin is the pipe — not the terminal.
# Re-attach to /dev/tty so install.sh can prompt interactively for SSID, password, etc.
# In non-interactive mode (INSTALL_NONINTERACTIVE=1) this is a no-op since install.sh
# never reads from stdin in that mode.
if [[ ! -t 0 ]] && [[ -e /dev/tty ]]; then
    exec bash "${INSTALL_SCRIPT}" </dev/tty
else
    exec bash "${INSTALL_SCRIPT}"
fi
