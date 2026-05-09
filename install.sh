#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║   pi-travel-router — Installer                                          ║
# ║   Raspberry Pi Zero 2 W + Pi OS Lite Bookworm                          ║
# ║   https://github.com/NicoMancinelli/pi-travel-router                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝
#
# Usage: sudo bash install.sh
# Run from the cloned repo root on a fresh Pi OS Lite Bookworm install.
# A reboot is required at the end to activate dwc2/g_ncm USB gadget mode.

set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="/var/log/firstboot-install.log"
install -m 600 -o root -g root /dev/null "$LOG" 2>/dev/null || true
exec > >(tee -a "$LOG") 2>&1

# ── Colours ───────────────────────────────────────────────────────────────────
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; C='\033[0;36m'; NC='\033[0m'; BLD='\033[1m'
ok()      { echo -e "${G}✓${NC} $*"; }
info()    { echo -e "${C}→${NC} $*"; }
warn()    { echo -e "${Y}⚠${NC} $*"; }
die()     { echo -e "${R}✗ FATAL:${NC} $*" >&2; exit 1; }
_STEP=0
section() { ((_STEP++)) || true; echo -e "\n${C}━━ [${_STEP}] $* ━━${NC}"; }
validate_flag() {
    local name=$1 value=${!1:-0}
    [[ "$value" =~ ^[01]$ ]] || die "$name must be 0 or 1"
}
# Try to detect WiFi country from system locale (e.g. en_GB.UTF-8 → GB)
_detect_country() {
    local _lc
    _lc="$(locale 2>/dev/null | grep '^LANG=' | sed 's/.*_\([A-Za-z][A-Za-z]\)[.@].*/\1/' | head -1 || true)"
    _lc="${_lc^^}"
    [[ "$_lc" =~ ^[A-Z]{2}$ ]] && echo "$_lc" || echo "US"
}

# ── Guards ────────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && die "Run as root: sudo bash install.sh"
[[ -f "$REPO/scripts/wan-watchdog.sh" ]] || die "Run from repo root (scripts/ not found)"
uname -m | grep -qE 'armv7l|aarch64' || warn "Expected armv7l/aarch64 — got $(uname -m)"
# Accept any Debian/Raspberry Pi OS release (bullseye, bookworm, trixie, …)
_os_id="$(grep '^ID=' /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"' || true)"
case "${_os_id}" in
    debian|raspbian) ;;
    *) warn "Unrecognised OS (${_os_id:-unknown}). Expected Raspberry Pi OS or Debian — continuing anyway." ;;
esac
unset _os_id

echo ""
echo "  Pi Zero 2 W Travel Router — Installer"
echo "  Log: $LOG"
echo ""

# ── Existing install detection ────────────────────────────────────────────────
_VERSION_FILE="/etc/travel-router-version"
_DEFAULTS_FILE="/etc/default/travel-router"
_INSTALL_ACTION="fresh"   # fresh | upgrade | reconfigure | repair | uninstall

# ── Uninstall function ────────────────────────────────────────────────────────
_uninstall() {
    section "Uninstalling pi-travel-router"
    warn "This removes all services, scripts, and config."
    warn "Installed packages (hostapd, dnsmasq, etc.) are left in place."
    echo ""
    read -rp "  Type 'yes' to confirm: " _uc
    [[ "$_uc" == "yes" ]] || { echo "Aborted."; exit 0; }

    info "Stopping and disabling services..."
    for _svc in \
        failover-watchdog.timer   failover-watchdog.service \
        wan-watchdog.timer        wan-watchdog.service \
        cpu-performance.service   cake-qdisc.service \
        wlan-mac-random.service \
        vnstat-metrics.timer      vnstat-metrics.service \
        update-blocklists.timer   update-blocklists.service \
        tailscale-watchdog.timer  tailscale-watchdog.service \
        wireguard-watchdog.timer  wireguard-watchdog.service \
        adguard-home.service \
        ap-disable.timer  ap-disable.service \
        ap-enable.timer   ap-enable.service \
        daily-digest.timer        daily-digest.service \
        update-router.timer       update-router.service \
        tune-cake.timer           tune-cake.service \
        ota-commit.timer          ota-commit.service \
        travel-router-firewall.service; do
        systemctl disable --now "$_svc" 2>/dev/null || true
        rm -f "/etc/systemd/system/$_svc"
    done
    systemctl daemon-reload
    ok "Systemd units removed"

    info "Removing scripts from /usr/local/sbin..."
    rm -f \
        /usr/local/sbin/wan-watchdog \
        /usr/local/sbin/failover-watchdog \
        /usr/local/sbin/travel-router-firewall \
        /usr/local/sbin/captive-check \
        /usr/local/sbin/travel-status \
        /usr/local/sbin/travel-tui \
        /usr/local/sbin/travel-diagnostic \
        /usr/local/sbin/update-router \
        /usr/local/sbin/update-router.sh \
        /usr/local/sbin/vnstat-metrics \
        /usr/local/sbin/vnstat-push \
        /usr/local/sbin/clone-mac \
        /usr/local/sbin/start-tether \
        /usr/local/sbin/stop-tether \
        /usr/local/sbin/start-bt-tether \
        /usr/local/sbin/stop-bt-tether \
        /usr/local/sbin/apply-split-tunnel \
        /usr/local/sbin/apply-cake \
        /usr/local/sbin/tune-cake \
        /usr/local/sbin/update-blocklists \
        /usr/local/sbin/generate-bandwidth-report \
        /usr/local/sbin/daily-digest \
        /usr/local/sbin/notify-router \
        /usr/local/sbin/ota-update \
        /usr/local/sbin/ota-commit \
        /usr/local/sbin/ota-rollback \
        /usr/local/sbin/setup-2fa.sh
    ok "Scripts removed"

    info "Removing config, data, and udev rules..."
    rm -f /etc/default/travel-router /etc/travel-router-version
    rm -rf /usr/local/share/travel-router
    rm -f /etc/udev/rules.d/90-ipheth.rules \
          /etc/udev/rules.d/91-android-tether.rules \
          /etc/udev/rules.d/99-apple-autosuspend.rules
    rm -f /etc/update-motd.d/10-travel-router
    udevadm control --reload-rules 2>/dev/null || true
    ok "Config and udev rules removed"

    echo ""
    ok "pi-travel-router uninstalled."
    info "Reboot recommended to restore default network settings."
    info "To also remove packages: sudo apt-get purge hostapd dnsmasq tailscale log2ram"
    echo ""
    exit 0
}

# ── Detect previous install and offer menu ────────────────────────────────────
if [[ "${INSTALL_NONINTERACTIVE:-0}" != "1" ]]; then
    if [[ -f "$_VERSION_FILE" ]]; then
        _INSTALLED_VER="$(cat "$_VERSION_FILE" 2>/dev/null || echo "unknown")"
        _REPO_VER="$(cat "$REPO/VERSION" 2>/dev/null || echo "unknown")"
        echo ""
        echo -e "  ${BLD}╔═══════════════════════════════════════════════════════╗${NC}"
        echo -e "  ${BLD}║   pi-travel-router is already installed               ║${NC}"
        echo    "  $(printf "${BLD}║   Installed: %-10s   Available: %-10s      ║${NC}" "$_INSTALLED_VER" "$_REPO_VER")"
        echo -e "  ${BLD}╚═══════════════════════════════════════════════════════╝${NC}"
        echo ""
        echo -e "  ${BLD}[1] Upgrade      ${NC}— update scripts, units, and packages (keep existing config)"
        echo -e "  ${BLD}[2] Reconfigure  ${NC}— change SSID, features, Tailscale key, and other settings"
        echo -e "  ${BLD}[3] Repair       ${NC}— re-run full install keeping all current settings"
        echo -e "  ${BLD}[4] Uninstall    ${NC}— remove pi-travel-router from this system"
        echo -e "  ${BLD}[5] Exit"
        echo ""
        read -rp "  Choice [1]: " _choice
        case "${_choice:-1}" in
            1) _INSTALL_ACTION="upgrade" ;;
            2) _INSTALL_ACTION="reconfigure" ;;
            3) _INSTALL_ACTION="repair" ;;
            4) _uninstall ;;
            5|q|Q) echo "Exiting."; exit 0 ;;
            *) warn "Invalid choice — defaulting to Upgrade"; _INSTALL_ACTION="upgrade" ;;
        esac
        echo ""
    elif [[ -f "$_DEFAULTS_FILE" ]]; then
        echo ""
        echo -e "  ${Y}╔═══════════════════════════════════════════════════════╗${NC}"
        echo -e "  ${Y}║   Partial install detected — previous run did not     ║${NC}"
        echo -e "  ${Y}║   finish. Config exists but no version stamp found.   ║${NC}"
        echo -e "  ${Y}╚═══════════════════════════════════════════════════════╝${NC}"
        echo ""
        echo -e "  ${BLD}[1] Resume       ${NC}— finish the interrupted install (recommended)"
        echo -e "  ${BLD}[2] Fresh start  ${NC}— wipe partial config and start from scratch"
        echo -e "  ${BLD}[3] Exit"
        echo ""
        read -rp "  Choice [1]: " _choice
        case "${_choice:-1}" in
            1) _INSTALL_ACTION="repair" ;;
            2) _INSTALL_ACTION="fresh"; rm -f "$_DEFAULTS_FILE" ;;
            3|q|Q) echo "Exiting."; exit 0 ;;
            *) warn "Invalid — defaulting to Resume"; _INSTALL_ACTION="repair" ;;
        esac
        echo ""
    fi
fi

# ── Load existing config for upgrade / repair / reconfigure ───────────────────
if [[ "$_INSTALL_ACTION" =~ ^(upgrade|repair|reconfigure)$ ]]; then
    # shellcheck disable=SC1090,SC1091
    source "$_DEFAULTS_FILE" 2>/dev/null || true
    # AP WiFi settings live in hostapd.conf, not the defaults file
    AP_SSID="${AP_SSID:-$(grep '^ssid=' /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2- || echo 'TravelRouter')}"
    AP_PASS="${AP_PASS:-$(grep '^wpa_passphrase=' /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2- || echo '')}"
    COUNTRY="${COUNTRY:-$(grep '^country_code=' /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2- || echo 'US')}"
    if [[ "$_INSTALL_ACTION" != "reconfigure" ]]; then
        # Upgrade / repair: skip all interactive prompts and re-use existing config
        [[ -n "${AP_PASS:-}" ]] || \
            die "Cannot read existing AP passphrase from /etc/hostapd/hostapd.conf. Use option 2 (Reconfigure) instead."
        INSTALL_NONINTERACTIVE=1
        export INSTALL_NONINTERACTIVE
    fi
fi

# ── Config prompts ────────────────────────────────────────────────────────────
section "Configuration"

# ── Prompt helpers ─────────────────────────────────────────────────────────────
# _yn    VAR "Question?"          "Tip"    — in reconfigure mode shows current and accepts Enter to keep
# _ask   VAR "Prompt" "default"   "Tip"    — in reconfigure mode prompts even when already set
# _secret VAR "Prompt"            "Tip"    — in reconfigure mode shows "(Enter to keep)" hint

_yn() {
    local v="${!1:-}"
    # Skip if already set — UNLESS we're reconfiguring (always re-prompt)
    if [[ "$v" =~ ^[01]$ ]] && [[ "${_INSTALL_ACTION:-fresh}" != "reconfigure" ]]; then return; fi
    if [[ "${INSTALL_NONINTERACTIVE:-0}" == "1" ]]; then
        [[ "$v" =~ ^[01]$ ]] || printf -v "$1" '%s' "0"; return
    fi
    [[ -n "${3:-}" ]] && echo -e "        ${C}ℹ${NC}  ${3}"
    local _yn_cur="${v:-0}"
    local _yn_hint="[y/N]"
    [[ "$_yn_cur" == "1" ]] && _yn_hint="[Y/n]"
    read -rp "  $2 ${_yn_hint} " _r
    if [[ -z "$_r" ]]; then
        printf -v "$1" '%s' "$_yn_cur"          # Enter = keep current
    else
        printf -v "$1" '%s' "$([[ "$_r" =~ ^[Yy]$ ]] && echo 1 || echo 0)"
    fi
}

_ask() {
    local cur="${!1:-}"
    # Skip if already set — UNLESS we're reconfiguring
    if [[ -n "$cur" ]] && [[ "${_INSTALL_ACTION:-fresh}" != "reconfigure" ]]; then return; fi
    local default="${3:-$cur}"
    [[ -n "${4:-}" ]] && echo -e "        ${C}ℹ${NC}  ${4}"
    if [[ -n "$default" ]]; then
        read -rp "  $2 [${default}]: " _r; printf -v "$1" '%s' "${_r:-${default}}"
    else
        read -rp "  $2: " _r; printf -v "$1" '%s' "${_r:-}"
    fi
}

_secret() {
    local cur="${!1:-}"
    # Skip if already set — UNLESS we're reconfiguring
    if [[ -n "$cur" ]] && [[ "${_INSTALL_ACTION:-fresh}" != "reconfigure" ]]; then return; fi
    [[ -n "${3:-}" ]] && echo -e "        ${C}ℹ${NC}  ${3}"
    if [[ -n "$cur" ]] && [[ "${_INSTALL_ACTION:-fresh}" == "reconfigure" ]]; then
        read -rsp "  $2 (Enter to keep current): " _r; echo
        [[ -z "$_r" ]] && return   # keep the existing value
    else
        read -rsp "  $2: " _r; echo
    fi
    printf -v "$1" '%s' "${_r:-}"
}

if [[ "${INSTALL_NONINTERACTIVE:-0}" != "1" ]]; then

    # ── Setup mode selector ─────────────────────────────────────────────────────
    if [[ "${_INSTALL_ACTION:-fresh}" == "reconfigure" ]]; then
        # Reconfigure: always show all groups so nothing is hidden
        _MODE="3"
        info "Showing all options. Press Enter on any prompt to keep the current value."
        echo ""
    else
        echo ""
        echo -e "  ${BLD}Choose a setup mode:${NC}\n"
        echo -e "  ${BLD}[1] Quick   ${NC}— just SSID + password; security features on by default    (2 min)"
        echo -e "  ${BLD}[2] Standard${NC}— adds Tailscale, SSH key, and security choices             (5 min)"
        echo -e "  ${BLD}[3] Expert  ${NC}— all options, grouped and explained                       (10 min)"
        echo ""
        read -rp "  Mode [1]: " _MODE
        _MODE="${_MODE:-1}"
        [[ "$_MODE" =~ ^[123]$ ]] || { warn "Invalid — defaulting to Quick"; _MODE="1"; }
        echo ""
    fi

    # ── Group 1: WiFi (all modes) ───────────────────────────────────────────────
    echo -e "  ${BLD}── WiFi ──────────────────────────────────────${NC}"

    # SSID — retry on invalid input
    while true; do
        _ask AP_SSID "Network name (SSID)" "TravelRouter" \
            "The name your devices see when connecting. 1–32 characters, no special symbols."
        if printf '%s' "${AP_SSID:-}" | LC_ALL=C grep -qP '[\x00-\x1f]'; then
            warn "Network name cannot contain control characters — try again."; AP_SSID=""; continue
        fi
        if [[ ${#AP_SSID} -lt 1 || ${#AP_SSID} -gt 32 ]]; then
            warn "Network name must be 1–32 characters (got ${#AP_SSID}) — try again."; AP_SSID=""; continue
        fi
        break
    done

    # Passphrase — retry on invalid, then confirm to catch typos
    while true; do
        _secret AP_PASS "WiFi password (8–63 chars, no #)" \
            "Used by all devices connecting to your network. Min 8 chars, no # character."
        if [[ ${#AP_PASS:-0} -lt 8 ]]; then
            warn "Password too short — minimum 8 characters. Try again."; AP_PASS=""; continue
        fi
        if [[ ${#AP_PASS} -gt 63 ]]; then
            warn "Password too long — maximum 63 characters. Try again."; AP_PASS=""; continue
        fi
        if printf '%s' "$AP_PASS" | LC_ALL=C grep -qP '[\x00-\x1f\x7f-\xff]'; then
            warn "Use printable ASCII characters only. Try again."; AP_PASS=""; continue
        fi
        if [[ "$AP_PASS" =~ '#' ]]; then
            warn "# is not allowed in the password. Try again."; AP_PASS=""; continue
        fi
        # Confirm — skip in reconfigure mode when user pressed Enter to keep existing
        if [[ "${_INSTALL_ACTION:-fresh}" != "reconfigure" ]]; then
            read -rsp "  Confirm password: " _AP_PASS2; echo
            if [[ "$AP_PASS" != "$_AP_PASS2" ]]; then
                warn "Passwords don't match — try again."; AP_PASS=""; continue
            fi
        fi
        break
    done

    # Country — auto-detect for Quick mode, always prompt for Standard/Expert
    _default_country="$(_detect_country)"
    if [[ "$_MODE" == "1" && -z "${COUNTRY:-}" ]]; then
        COUNTRY="$_default_country"
        if [[ "$COUNTRY" == "US" ]]; then
            echo -e "        ${C}ℹ${NC}  Country defaulted to US. Change with: sudo travel-tui"
        else
            echo -e "        ${C}ℹ${NC}  Country auto-detected: ${COUNTRY}. Change with: sudo travel-tui"
        fi
    else
        while true; do
            _ask COUNTRY "WiFi country code" "${_default_country}" \
                "Two-letter code (US, GB, DE, AU, JP…). Required for legal WiFi channel settings."
            COUNTRY="${COUNTRY^^}"
            [[ "$COUNTRY" =~ ^[A-Z]{2}$ ]] && break
            warn "Country code must be exactly two letters (e.g. US, GB, DE) — try again."
            COUNTRY=""
        done
    fi
    echo ""

    if [[ "$_MODE" == "1" ]]; then
        # Quick: lock in sensible security defaults; skip all further prompts
        ENABLE_AUTO_UPDATES="${ENABLE_AUTO_UPDATES:-1}"
        ENABLE_ADGUARD="${ENABLE_ADGUARD:-1}"
        ENABLE_DOT="${ENABLE_DOT:-1}"
        ENABLE_BLOCKLISTS="${ENABLE_BLOCKLISTS:-1}"
        ENABLE_VPN_KILLSWITCH="${ENABLE_VPN_KILLSWITCH:-0}"
        echo -e "  ${G}✓${NC} Security defaults applied (AdGuard ON, DoT ON, auto-updates ON, blocklists ON)"
        echo -e "  ${C}→${NC} Re-run with mode 2 or 3 to customise Tailscale, SSH keys, and advanced features."
        echo ""
    fi

    if [[ "$_MODE" =~ ^[23]$ ]]; then
        # ── Group 2: Remote access ───────────────────────────────────────────────
        echo -e "  ${BLD}── Remote Access ─────────────────────────────${NC}"
        _secret TS_KEY "Tailscale auth key (tskey-auth-… or blank to skip)" \
            "Get one at tailscale.com/settings/keys. Adds this Pi to your tailnet automatically."
        _ask HEADSCALE_URL "Headscale URL (blank = Tailscale cloud)" "" \
            "Only if you self-host Headscale. Leave blank for the standard Tailscale service."
        _ask SSH_ADMIN_KEY "SSH public key (paste pubkey, or blank for password auth)" "" \
            "Run 'cat ~/.ssh/id_ed25519.pub' on your laptop and paste it here. Disables password SSH."
        _ask NTFY_TOPIC "ntfy.sh push notification topic (blank = off)" "" \
            "Create a free topic at ntfy.sh. Alerts you on failovers, reboots, and security events."
        echo ""

        # ── Group 3: Security ────────────────────────────────────────────────────
        echo -e "  ${BLD}── Security ──────────────────────────────────${NC}"
        _yn ENABLE_AUTO_UPDATES   "Auto OS security updates?"      \
            "Installs security patches nightly via unattended-upgrades. Strongly recommended."
        _yn ENABLE_ADGUARD        "AdGuard Home (DNS ad-blocker)?" \
            "Blocks ads + trackers for every connected device. Dashboard at http://192.168.7.1:3000"
        _yn ENABLE_DOT            "DNS-over-TLS?"                  \
            "Encrypts DNS via stubby → Cloudflare 1.1.1.1 + Quad9. Prevents DNS snooping on hotel WiFi."
        _yn ENABLE_BLOCKLISTS     "Threat-intel IP blocklist?"     \
            "Blocks Firehol Level-1: known malware, botnets, and scanner IPs (~10 000 entries)."
        _yn ENABLE_VPN_KILLSWITCH "VPN kill switch?"               \
            "Drops all AP traffic if Tailscale disconnects. Only enable if you always route via VPN."
        echo ""
    fi

    if [[ "$_MODE" == "3" ]]; then
        # ── Group 4: Privacy ─────────────────────────────────────────────────────
        echo -e "  ${BLD}── Privacy ───────────────────────────────────${NC}"
        _yn ENABLE_TOR_TRANSPARENT    "Tor transparent proxy?"           \
            "Routes all AP traffic through Tor. Adds a second 'TorAP' SSID. Slow but anonymous."
        _yn ENABLE_HTTP_UA_REWRITE    "HTTP User-Agent normalisation?"   \
            "Rewrites browser fingerprints via privoxy. Reduces cross-site tracking."
        _yn ENABLE_OPEN_WIFI_FALLBACK "Auto-join open WiFi networks?"    \
            "Connects to any open AP when no known network is in range. Convenient but risky."
        echo ""

        # ── Group 5: Network ─────────────────────────────────────────────────────
        echo -e "  ${BLD}── Network ───────────────────────────────────${NC}"
        _yn ENABLE_CLIENT_QOS     "Per-client bandwidth fairness (CAKE)?" \
            "Prevents one device hogging the uplink. Uses CAKE qdisc per-host on the AP interface."
        _yn ENABLE_CAKE_AUTOTUNE  "Auto CAKE bandwidth tuning?"           \
            "Runs a weekly speedtest to calibrate CAKE shaper rates automatically."
        _yn ENABLE_SPLIT_TUNNEL   "Domain-based split tunnel?"            \
            "Route specific domains (e.g. work.example.com) via Tailscale; everything else direct."
        if [[ "${ENABLE_SPLIT_TUNNEL:-0}" = "1" && -z "${SPLIT_TUNNEL_DOMAINS:-}" ]]; then
            _ask SPLIT_TUNNEL_DOMAINS "Split tunnel domains (space-separated)" "" \
                "Example: mybank.com work.example.com — these go via your tailnet, rest goes direct."
        fi
        _yn ENABLE_AVAHI_REFLECTOR "mDNS reflector (AirPrint / AirPlay)?" \
            "Reflects mDNS across subnets so AirPrint and AirPlay work over Tailscale."
        _yn ENABLE_PER_DEVICE_VPN  "Per-device VPN routing?"              \
            "Route specific device MACs through Tailscale; others go direct to the internet."
        echo ""

        # ── Group 6: Schedule & Services ─────────────────────────────────────────
        echo -e "  ${BLD}── Schedule & Services ───────────────────────${NC}"
        _yn ENABLE_AP_SCHEDULE         "Scheduled AP quiet hours (02:00–07:00)?" \
            "Turns the WiFi AP off overnight to save power and reduce RF exposure."
        _yn ENABLE_2FA                 "SSH two-factor auth (TOTP)?"             \
            "Adds a Google Authenticator OTP on top of SSH key auth. Requires an authenticator app."
        _yn ENABLE_BANDWIDTH_DASHBOARD "Daily bandwidth report?"                 \
            "Generates an HTML traffic report per device each day. Viewable on the web dashboard."
        _yn ENABLE_PROMETHEUS_EXPORTER "Prometheus metrics exporter on :9100?"   \
            "Node exporter for Grafana/Prometheus. Only needed if you run a monitoring stack."
        _yn ENABLE_UPS_MONITOR         "PiSugar UPS battery monitor?"            \
            "Monitors a PiSugar battery HAT and triggers a safe shutdown at low charge."
        echo ""

        if [[ "${ENABLE_TOR_TRANSPARENT:-0}" = "1" && -z "${TOR_AP_PASS:-}" ]]; then
            _secret TOR_AP_PASS "Tor AP passphrase (8+ chars, for the TorAP SSID)" \
                "A separate passphrase for the Tor-only SSID. Must be 8+ chars, no #."
            [[ ${#TOR_AP_PASS} -ge 8 ]] || die "Tor AP passphrase must be 8+ characters"
        fi
    fi

fi

# Defaults for both interactive and non-interactive paths.
AP_SSID="${AP_SSID:-TravelRouter}"
COUNTRY="${COUNTRY:-US}"
NTFY_TOPIC="${NTFY_TOPIC:-}"
TS_KEY="${TS_KEY:-}"
SSH_ADMIN_KEY="${SSH_ADMIN_KEY:-}"
HEADSCALE_URL="${HEADSCALE_URL:-}"
SPLIT_TUNNEL_DOMAINS="${SPLIT_TUNNEL_DOMAINS:-}"
TOR_AP_PASS="${TOR_AP_PASS:-}"

if [[ "${INSTALL_NONINTERACTIVE:-0}" == "1" ]]; then
    [[ -n "${AP_PASS:-}" ]] || die "Set AP_PASS in environment for non-interactive install"
    # Strip newlines from CLI-supplied passwords to prevent config-file injection
    AP_PASS="${AP_PASS//$'\n'/}"
    AP_PASS="${AP_PASS//$'\r'/}"
    TOR_AP_PASS="${TOR_AP_PASS//$'\n'/}"
    TOR_AP_PASS="${TOR_AP_PASS//$'\r'/}"
    : "${ENABLE_BLOCKLISTS:=0}"
    : "${ENABLE_TOR_TRANSPARENT:=0}"
    : "${ENABLE_HTTP_UA_REWRITE:=0}"
    : "${ENABLE_OPEN_WIFI_FALLBACK:=0}"
    : "${ENABLE_DOT:=0}"
    : "${ENABLE_VPN_KILLSWITCH:=0}"
    : "${ENABLE_AUTO_UPDATES:=0}"
    : "${ENABLE_ADGUARD:=0}"
    : "${ENABLE_AVAHI_REFLECTOR:=0}"
    : "${ENABLE_AP_SCHEDULE:=0}"
    : "${ENABLE_CLIENT_QOS:=0}"
    : "${ENABLE_PER_DEVICE_VPN:=0}"
    : "${ENABLE_CAKE_AUTOTUNE:=0}"
    : "${ENABLE_SPLIT_TUNNEL:=0}"
    : "${ENABLE_2FA:=0}"
    : "${ENABLE_BANDWIDTH_DASHBOARD:=0}"
    : "${ENABLE_PROMETHEUS_EXPORTER:=0}"
    : "${ENABLE_UPS_MONITOR:=0}"
    : "${ENABLE_WIREGUARD:=0}"
    : "${WG_LISTEN_PORT:=51820}"
    if [[ "${ENABLE_TOR_TRANSPARENT}" == "1" ]]; then
        [[ ${#TOR_AP_PASS} -ge 8 ]] || die "Set TOR_AP_PASS (8+ chars) in environment when ENABLE_TOR_TRANSPARENT=1"
    fi
fi

[[ -n "$AP_SSID" && ${#AP_SSID} -le 32 ]] || die "SSID must be 1-32 characters"
[[ ${#AP_PASS} -ge 8 && ${#AP_PASS} -le 63 ]] || die "Passphrase must be 8-63 characters"
[[ "$AP_PASS" =~ '#' ]] && die "AP passphrase must not contain '#' (hostapd comment character)"
[[ "${TOR_AP_PASS:-}" =~ '#' ]] && die "Tor AP passphrase must not contain '#' (hostapd comment character)"
[[ "$COUNTRY" =~ ^[A-Za-z]{2}$ ]] || die "Country code must be two letters, e.g. US"
COUNTRY="${COUNTRY^^}"
[[ "$NTFY_TOPIC" =~ ^[A-Za-z0-9._-]*$ ]] || die "ntfy.sh topic may only contain letters, numbers, dot, underscore, or dash"
if [[ -n "$TS_KEY" && -z "$HEADSCALE_URL" ]]; then
    [[ "$TS_KEY" =~ ^tskey-auth- ]] || die "Tailscale auth key must start with tskey-auth-"
fi
ENABLE_WAN_METRICS="${ENABLE_WAN_METRICS:-1}"
for flag in ENABLE_OPEN_WIFI_FALLBACK ENABLE_HTTP_UA_REWRITE ENABLE_TOR_TRANSPARENT ENABLE_BLOCKLISTS ENABLE_DOT ENABLE_VPN_KILLSWITCH ENABLE_AUTO_UPDATES ENABLE_AVAHI_REFLECTOR ENABLE_ADGUARD ENABLE_AP_SCHEDULE ENABLE_CLIENT_QOS ENABLE_PER_DEVICE_VPN ENABLE_CAKE_AUTOTUNE ENABLE_SPLIT_TUNNEL ENABLE_2FA ENABLE_WAN_METRICS ENABLE_BANDWIDTH_DASHBOARD ENABLE_PROMETHEUS_EXPORTER ENABLE_UPS_MONITOR ENABLE_WIREGUARD; do
    validate_flag "$flag"
done

# Validate ROUTER_HOSTNAME when supplied via environment (direct-run path)
# Regex requires alphanumeric start AND end, disallowing trailing hyphens (RFC 952).
if [[ -n "${ROUTER_HOSTNAME:-}" ]]; then
    [[ "$ROUTER_HOSTNAME" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]] || \
        die "ROUTER_HOSTNAME '${ROUTER_HOSTNAME}' is invalid — use only letters, numbers, and hyphens (max 63 chars); must not start or end with a hyphen"
fi

# Validate AP schedule times when supplied via environment (direct-run path).
# The wizard prompts accept freeform input but install.sh writes them straight
# into a systemd OnCalendar= directive — a newline or extra chars would inject
# arbitrary directives into the drop-in file.
if [[ -n "${AP_DISABLE_TIME:-}" ]] && ! [[ "$AP_DISABLE_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    die "AP_DISABLE_TIME must be in HH:MM format (00:00–23:59)"
fi
if [[ -n "${AP_ENABLE_TIME:-}" ]] && ! [[ "$AP_ENABLE_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    die "AP_ENABLE_TIME must be in HH:MM format (00:00–23:59)"
fi

if [[ "${INSTALL_NONINTERACTIVE:-0}" != "1" ]]; then
    echo ""
    echo -e "  ${BLD}╔═════════════════════════════════════════════════════╗${NC}"
    echo -e "  ${BLD}║   Ready to install                                  ║${NC}"
    echo -e "  ${BLD}╚═════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${BLD}WiFi network${NC}"
    echo -e "    Name      ${BLD}${AP_SSID}${NC}"
    echo -e "    Country   ${COUNTRY}"
    echo ""
    echo -e "  ${BLD}Security${NC}"
    # shellcheck disable=SC2031
    _sum_feat() { [[ "${!1:-0}" == "1" ]] && echo -e "    ${G}✓${NC}  $2" || echo -e "    ${C}·${NC}  $2  (off)"; }
    _sum_feat ENABLE_AUTO_UPDATES   "Automatic OS security updates"
    _sum_feat ENABLE_ADGUARD        "AdGuard Home  (DNS ad-blocker, http://192.168.7.1:3000)"
    _sum_feat ENABLE_DOT            "DNS-over-TLS  (encrypted DNS via Cloudflare + Quad9)"
    _sum_feat ENABLE_BLOCKLISTS     "Threat-intel IP blocklist  (Firehol Level 1)"
    [[ "${ENABLE_VPN_KILLSWITCH:-0}" == "1" ]] && \
        echo -e "    ${G}✓${NC}  VPN kill switch  (AP blocked if Tailscale drops)"
    echo ""
    if [[ -n "${TS_KEY:-}" ]]; then
        echo -e "  ${G}✓${NC}  Tailscale key provided — Pi will join your tailnet on first boot"
    else
        echo -e "  ${C}→${NC}  Tailscale: run ${BLD}sudo tailscale up${NC} after reboot to connect"
    fi
    [[ -n "${SSH_ADMIN_KEY:-}" ]] && \
        echo -e "  ${G}✓${NC}  SSH public key loaded — password auth will be disabled"
    echo ""
    echo -e "  ${Y}This will take 8–15 minutes. A reboot is required at the end.${NC}"
    echo ""
    # shellcheck disable=SC2034
    read -rp "  Press Enter to start, Ctrl+C to cancel... " _ignored
    echo ""
fi

# ── Apply hostname ────────────────────────────────────────────────────────────
if [[ -n "${ROUTER_HOSTNAME:-}" && "$ROUTER_HOSTNAME" != "travelrouter" ]]; then
    hostnamectl set-hostname "$ROUTER_HOSTNAME" 2>/dev/null || true
    # L11: use Python for safe hostname substitution (avoids regex metacharacter issues)
    python3 -c "
import sys, tempfile, os
with open('/etc/hosts') as f: content = f.read()
new = content.replace('travelrouter', sys.argv[1])
fd, tmp = tempfile.mkstemp(dir='/etc')
try:
    with os.fdopen(fd, 'w') as f: f.write(new)
    os.replace(tmp, '/etc/hosts')
except:
    os.unlink(tmp)
    raise
" "$ROUTER_HOSTNAME" 2>/dev/null || true
    echo "$ROUTER_HOSTNAME" > /etc/hostname
    ok "Hostname set to $ROUTER_HOSTNAME"
fi

# ── Apply timezone ────────────────────────────────────────────────────────────
if [[ -n "${ROUTER_TIMEZONE:-}" ]]; then
    [[ "$ROUTER_TIMEZONE" =~ ^[A-Za-z][A-Za-z0-9/_+-]{1,49}$ ]] || die "Invalid ROUTER_TIMEZONE: $ROUTER_TIMEZONE"
    timedatectl set-timezone "$ROUTER_TIMEZONE" 2>/dev/null || true
    ok "Timezone set to $ROUTER_TIMEZONE"
fi

# ── Helpers ───────────────────────────────────────────────────────────────────
install_file() {
    # install_file <src-in-repo> <dest> [mode]
    local src="$REPO/$1" dst="$2" mode="${3:-644}"
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    chmod "$mode" "$dst"
}

# ── 1. Packages ───────────────────────────────────────────────────────────────
section "Installing packages"

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    hostapd dnsmasq iptables iptables-persistent netfilter-persistent \
    curl wget git jq \
    usbmuxd libimobiledevice6 libimobiledevice-utils ipheth-utils \
    macchanger vnstat \
    privoxy \
    tor \
    stubby \
    unattended-upgrades \
    bluez bluez-tools python3-dbus \
    avahi-daemon \
    iproute2 iw wireless-tools \
    qrencode \
    radvd

ok "Packages installed"

# log2ram
if ! dpkg -l log2ram &>/dev/null; then
    info "Installing log2ram"
    echo "deb [signed-by=/usr/share/keyrings/azlux-archive-keyring.gpg] http://packages.azlux.fr/debian/ bookworm main" \
        > /etc/apt/sources.list.d/azlux.list
    # S-H8: download, dearmor, verify fingerprint before trusting
    curl -s https://azlux.fr/repo.gpg.key | gpg --dearmor -o /tmp/azlux.gpg
    _AZLUX_FP=$(gpg --no-default-keyring --keyring /tmp/azlux.gpg --fingerprint 2>/dev/null | tr -d ' \n' | grep -oi '[0-9A-F]\{40\}' | head -1 || true)
    _AZLUX_EXPECTED="7ACDC3E7BB726C780FFA4C5C6C26D5E78B89A06B"
    if [[ "${_AZLUX_FP^^}" != "$_AZLUX_EXPECTED" ]]; then
        rm -f /tmp/azlux.gpg
        die "log2ram GPG key fingerprint mismatch — aborting (got: ${_AZLUX_FP:-empty})"
    fi
    mv /tmp/azlux.gpg /usr/share/keyrings/azlux-archive-keyring.gpg
    chmod 644 /usr/share/keyrings/azlux-archive-keyring.gpg
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y log2ram
fi
ok "log2ram installed"

# Tailscale
if ! command -v tailscale &>/dev/null; then
    info "Installing Tailscale"
    curl -fsSL https://tailscale.com/install.sh | sh
fi
ok "Tailscale installed"

# RaspAP
if ! dpkg -l raspap-webgui &>/dev/null && [[ ! -d /etc/raspap ]]; then
    info "Installing RaspAP"
    curl -sL https://install.raspap.com | bash -s -- --yes --wireguard 0 --ad-blocker 0 --openvpn 0
    ok "RaspAP installed"
else
    ok "RaspAP already present — skipping"
fi

# S-H3: rotate RaspAP default credentials immediately after install
RASPAP_PASS=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16 || true)
if [[ -n "$RASPAP_PASS" ]]; then
    _RASPAP_AUTH=""
    for _p in /etc/raspap/raspap.auth /var/www/html/app/config/raspap.php /etc/raspap/hostapd/auth.conf; do
        [[ -f "$_p" ]] && { _RASPAP_AUTH="$_p"; break; }
    done
    if [[ -n "$_RASPAP_AUTH" ]]; then
        # Replace password field — file format varies; use Python for safety
        python3 -c "
import sys, re
path, pwd = sys.argv[1], sys.argv[2]
with open(path) as f: content = f.read()
content = re.sub(r'(password|pass)\s*=\s*[\"']?[A-Za-z0-9!@#\$%^&*_-]*[\"']?', r'\1 = \"' + pwd + '\"', content, flags=re.IGNORECASE)
with open(path, 'w') as f: f.write(content)
" "$_RASPAP_AUTH" "$RASPAP_PASS" 2>/dev/null || true
        ok "RaspAP password rotated (stored in $_RASPAP_AUTH)"
    else
        # Fallback: write to known PHP config location used by raspi-webgui
        _RASPAP_AUTH_DIR="/etc/raspap"
        mkdir -p "$_RASPAP_AUTH_DIR"
        printf 'admin:%s\n' "$RASPAP_PASS" > "$_RASPAP_AUTH_DIR/raspap.auth"
        chmod 640 "$_RASPAP_AUTH_DIR/raspap.auth"
        ok "RaspAP auth file created at $_RASPAP_AUTH_DIR/raspap.auth"
    fi
fi

# ── 2. Boot config (USB gadget mode) ─────────────────────────────────────────
section "Boot config — USB gadget mode (dwc2/g_ncm)"

CONFIG_TXT="/boot/firmware/config.txt"
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="/boot/config.txt"

if ! grep -q "dtoverlay=dwc2" "$CONFIG_TXT"; then
    { echo ""; echo "[all]"; echo "dtoverlay=dwc2,dr_mode=peripheral"; echo "dtoverlay=watchdog"; } >> "$CONFIG_TXT"
    ok "dwc2 overlay added to $CONFIG_TXT"
else
    ok "dwc2 overlay already present"
fi

echo "dwc2"  > /etc/modules-load.d/dwc2.conf
# g_ncm (CDC NCM) used instead of g_ether (CDC ECM): Windows 10/11 inbox NCM driver
echo "g_ncm" > /etc/modules-load.d/g-ncm.conf
echo "tcp_bbr" > /etc/modules-load.d/tcp_bbr.conf
echo "bcm2835_wdt" > /etc/modules-load.d/watchdog.conf
ok "Module load configs written"

# ── 3. Sysctl ─────────────────────────────────────────────────────────────────
section "Sysctl — forwarding, BBR, IPv6 uplink disable"

cat > /etc/sysctl.d/99-tailscale.conf << 'EOF'
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF

cat > /etc/sysctl.d/99-disable-ipv6-uplink.conf << 'EOF'
net.ipv6.conf.wlan0.disable_ipv6 = 1
net.ipv6.conf.eth0.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
EOF

# I-H3: write BBR settings to drop-in file instead of appending to /etc/sysctl.conf
cat > /etc/sysctl.d/99-bbr.conf << 'EOF'
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF

sysctl -p /etc/sysctl.d/99-tailscale.conf &>/dev/null || true
sysctl -p /etc/sysctl.d/99-disable-ipv6-uplink.conf &>/dev/null || true
ok "Sysctl configured"

# ── 4. NetworkManager — usb0 static IP (USB gadget) ─────────────────────────
section "NetworkManager — usb0 static IP (USB gadget)"

USB0_CONN="/etc/NetworkManager/system-connections/usb0-firstboot.nmconnection"
if [[ ! -f "$USB0_CONN" ]]; then
    install_file config/usb0-firstboot.nmconnection "$USB0_CONN" 600
    nmcli connection reload 2>/dev/null || true
    ok "usb0 NM profile installed"
else
    ok "usb0 NM profile already present"
fi

# ── 5. NetworkManager — wifi power save off + MAC randomization ───────────────
section "NetworkManager config"

mkdir -p /etc/NetworkManager/conf.d
install_file config/NetworkManager-wifi-random-mac.conf /etc/NetworkManager/conf.d/wifi-random-mac.conf

cat > /etc/NetworkManager/conf.d/wifi-powersave.conf << 'EOF'
# Disable WiFi power save on uplink STA interface
# PSM causes 100-200ms latency spikes and triggers iOS hotspot sleep
[connection]
wifi.powersave = 2
EOF
ok "NetworkManager: MAC randomization + power save off"

# ── 6. brcmfmac driver tuning ─────────────────────────────────────────────────
section "brcmfmac — disable firmware roaming engine"

cat > /etc/modprobe.d/brcmfmac.conf << 'EOF'
# Hand roaming control to wpa_supplicant (roamoff=1)
# Disable SAE offload + SWSUP to prevent auth failures in AP/STA concurrent mode
options brcmfmac roamoff=1 feature_disable=0x82000
EOF
ok "brcmfmac roamoff=1 feature_disable=0x82000"

# ── 7. wpa_supplicant — optional open WiFi fallback ──────────────────────────
section "wpa_supplicant — optional open WiFi fallback"

if [[ "${ENABLE_OPEN_WIFI_FALLBACK:-0}" = "1" && ! -f /etc/wpa_supplicant/wpa_supplicant.conf ]]; then
    cat > /etc/wpa_supplicant/wpa_supplicant.conf << EOF
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=$COUNTRY

# Open network fallback — connects to any open WiFi when no saved network found
# priority=1 is lowest; RaspAP-managed networks get higher priorities
network={
    key_mgmt=NONE
    priority=1
    scan_ssid=0
    id_str="open-fallback"
}
EOF
    chmod 600 /etc/wpa_supplicant/wpa_supplicant.conf
    ok "wpa_supplicant open fallback configured"
elif [[ "${ENABLE_OPEN_WIFI_FALLBACK:-0}" = "1" ]]; then
    warn "wpa_supplicant.conf already exists — open fallback not modified"
    warn "Manually add: network={key_mgmt=NONE priority=1 id_str=\"open-fallback\"}"
else
    ok "Open WiFi fallback disabled by default"
fi

# ── 8. hostapd ────────────────────────────────────────────────────────────────
section "hostapd — 802.11n + DTIM tuning"

if [[ -f /etc/hostapd/hostapd.conf ]]; then
    # I-H2: config already exists — read back SSID/pass rather than overwriting
    _EXISTING_SSID=$(python3 -c "
import sys
with open('/etc/hostapd/hostapd.conf') as f:
    for l in f:
        if l.startswith('ssid='):
            print(l.strip()[5:])
            break
" 2>/dev/null || true)
    _EXISTING_PASS=$(python3 -c "
import sys
with open('/etc/hostapd/hostapd.conf') as f:
    in_bss = False
    for l in f:
        if l.startswith('bss='): in_bss = True
        if not in_bss and l.startswith('wpa_passphrase='):
            print(l.strip()[15:])
            break
" 2>/dev/null || true)
    if [[ -n "$_EXISTING_SSID" ]]; then
        warn "hostapd.conf already exists — preserving existing SSID/passphrase"
        AP_SSID="${AP_SSID:-$_EXISTING_SSID}"
        AP_PASS="${AP_PASS:-$_EXISTING_PASS}"
    fi
    ok "hostapd.conf already present — not overwritten (SSID=$AP_SSID)"
else
    cat > /etc/hostapd/hostapd.conf << EOF
driver=nl80211
ctrl_interface=/var/run/hostapd
ctrl_interface_group=0
beacon_int=100
auth_algs=1
wpa_key_mgmt=WPA-PSK
ssid=PLACEHOLDER_SSID
channel=6
# TODO: 5GHz support requires hw_mode=a and channel=36+ — add WIFI_CHANNEL and WIFI_HW_MODE config vars
hw_mode=g
wpa_passphrase=PLACEHOLDER_PASS
interface=uap0
wpa=2
wpa_pairwise=CCMP
country_code=$COUNTRY

# 802.11n — [SHORT-GI-40] omitted: not supported by brcmfmac on Zero 2 W
ieee80211n=1
wmm_enabled=1
ht_capab=[HT40][SHORT-GI-20][DSSS_CCK-40]

# DTIM=1: iOS wakes every beacon, halves interactive latency
dtim_period=1
EOF
    # C6: write SSID and passphrase safely via Python to avoid shell quoting issues
    python3 -c "
import sys, os, tempfile
path = '/etc/hostapd/hostapd.conf'
with open(path) as f: lines = f.readlines()
out = []
for l in lines:
    if l.startswith('ssid='): out.append('ssid=' + sys.argv[1] + '\n')
    elif l.startswith('wpa_passphrase='): out.append('wpa_passphrase=' + sys.argv[2] + '\n')
    else: out.append(l)
fd, tmp = tempfile.mkstemp(dir='/etc/hostapd', prefix='hostapd.conf.')
try:
    with os.fdopen(fd, 'w') as fh: fh.writelines(out)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except:
    os.unlink(tmp); raise
" "$AP_SSID" "$AP_PASS"
    ok "hostapd configured: SSID=$AP_SSID"
fi

# Apply regulatory domain immediately (takes effect without reboot).
# hostapd also enforces country_code= at startup, so this is belt-and-braces.
if command -v iw &>/dev/null; then
    if iw reg set "$COUNTRY"; then
        ok "iw: regulatory domain set to $COUNTRY"
    else
        warn "iw reg set $COUNTRY failed (non-fatal; hostapd will enforce it at boot)"
    fi
fi

# ── 9. dnsmasq ────────────────────────────────────────────────────────────────
section "dnsmasq config"

install_file config/dnsmasq-travel-tweaks.conf /etc/dnsmasq.d/travel-tweaks.conf

# Add DNS rebinding protection if not already there
if ! grep -q "stop-dns-rebind" /etc/dnsmasq.d/travel-tweaks.conf; then
    cat >> /etc/dnsmasq.d/travel-tweaks.conf << 'EOF'

# DNS rebinding protection
stop-dns-rebind
rebind-localhost-ok
rebind-domain-ok=/local/
rebind-domain-ok=/lan/
EOF
fi

install_file config/dnsmasq-usb-gadget.conf  /etc/dnsmasq.d/usb-gadget.conf
install_file config/dnsmasq-static-leases.conf /etc/dnsmasq.d/static-leases.conf
ok "dnsmasq configs installed"

# ── 9b. stubby — DNS-over-TLS ────────────────────────────────────────────────
section "stubby — DNS-over-TLS"

mkdir -p /etc/stubby
install_file config/stubby.yml /etc/stubby/stubby.yml 644

if [[ "${ENABLE_DOT:-0}" = "1" ]]; then
    install_file config/dnsmasq-dot.conf /etc/dnsmasq.d/dot.conf
    systemctl enable --now stubby 2>/dev/null || true
    ok "DNS-over-TLS enabled: dnsmasq → stubby → Cloudflare/Quad9"
else
    systemctl disable --now stubby 2>/dev/null || true
    ok "DNS-over-TLS installed but disabled (set ENABLE_DOT=1 to activate)"
fi

# ── 10. rc.local ──────────────────────────────────────────────────────────────
section "rc.local — AP interface + channel sync + power save"

install_file config/rc.local /etc/rc.local 755
ok "rc.local installed"

# ── 11. Scripts ───────────────────────────────────────────────────────────────
section "Scripts → /usr/local/bin/"

for script in \
    start-tether.sh stop-tether.sh \
    failover-watchdog.sh wan-watchdog.sh captive-check.sh \
    notify-router.sh apply-cake.sh \
    vnstat-metrics.sh update-blocklists.sh travel-router-firewall.sh \
    start-bt-tether.sh stop-bt-tether.sh \
    clone-mac.sh \
    ap-schedule.sh \
    update-router.sh \
    tailscale-watchdog.sh \
    wireguard-watchdog.sh \
    travel-status.sh \
    daily-digest.sh; do
    install_file "scripts/$script" "/usr/local/bin/$script" 755
    ok "  $script"
done

install_file scripts/travel-diagnostic.sh /usr/local/bin/travel-diagnostic 755

# ── TUI: Python (preferred) + bash fallback ───────────────────────────────────
# Install Python TUI
cp "${REPO}/scripts/travel-tui.py" /usr/local/sbin/travel-tui.py
chmod 0755 /usr/local/sbin/travel-tui.py
ok "  travel-tui.py → /usr/local/sbin/travel-tui.py"

# Keep bash TUI as fallback
cp "${REPO}/scripts/travel-tui-legacy.sh" /usr/local/sbin/travel-tui-legacy
chmod 0755 /usr/local/sbin/travel-tui-legacy
ok "  travel-tui-legacy.sh → /usr/local/sbin/travel-tui-legacy"

# Update /usr/local/sbin/travel-tui to prefer Python TUI, fall back to bash
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
ok "  travel-diagnostic.sh → travel-diagnostic"

# Captive portal per-SSID hooks directory.
# Drop a script named after your hotel SSID (spaces/slashes → _) here to
# automate captive portal login for that network.  Example:
#   sudo cp /etc/travel-router/portals/examples/example-accept-terms.sh \
#       /etc/travel-router/portals/MyHotelSSID.sh
#   sudo chmod +x /etc/travel-router/portals/MyHotelSSID.sh
# See /etc/travel-router/portals/examples/ and scripts/portals/README.md for details.
mkdir -p /etc/travel-router/portals/examples
# I-H5: restrict portal dirs — scripts may contain credentials
chmod 0750 /etc/travel-router/portals
chmod 0750 /etc/travel-router/portals/examples

# Copy example portal scripts for reference (not auto-loaded — examples only)
if [[ -d "$REPO/scripts/portals" ]]; then
    for f in "$REPO"/scripts/portals/*.sh; do
        [[ -f "$f" ]] || continue
        cp "$f" /etc/travel-router/portals/examples/
        # I-H5: 0640 — readable by group only; deployed scripts with credentials must be chmod 600
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
#    # Note iPhone MAC when it appears (format: XX:XX:XX:XX:XX:XX)
#    pair XX:XX:XX:XX:XX:XX
#    trust XX:XX:XX:XX:XX:XX
#    quit
# 3. Set IPHONE_BT_MAC="XX:XX:XX:XX:XX:XX" in /etc/default/travel-router
# 4. Connect: sudo /usr/local/bin/start-bt-tether.sh
# 5. iPhone shows "Bluetooth" badge in status bar (not "Personal Hotspot" count)
EOF

# ── 12. /etc/default/travel-router ───────────────────────────────────────────
section "Travel router config defaults"

install_file config/travel-router-defaults /etc/default/travel-router 600

# Helper: safely rewrite a key=value line in a file using Python (C11).
# Handles values containing |, \, ", and other shell metacharacters.
_safe_write_conf() {
    local key="$1" val="$2" path="$3"
    python3 -c "
import sys, re, shlex, os, tempfile
key, val, path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f: lines = f.readlines()
pat = re.compile(r'^' + re.escape(key) + r'=')
new_line = key + '=' + shlex.quote(val) + '\n'
lines = [new_line if pat.match(l) else l for l in lines]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
try:
    with os.fdopen(fd, 'w') as fh: fh.writelines(lines)
    os.replace(tmp, path)
except:
    os.unlink(tmp); raise
" "$key" "$val" "$path"
}

DEFAULTS_FILE="/etc/default/travel-router"
ENABLE_WAN_METRICS="${ENABLE_WAN_METRICS:-1}"
# I-M5: AP subnet variables — sourced from defaults file if already written, else use built-in defaults
AP_SUBNET="${AP_SUBNET:-10.3.141.0/24}"
AP_GATEWAY="${AP_GATEWAY:-10.3.141.1}"

# Write boolean flags (values are always 0 or 1 — safe with sed too, but use helper for consistency)
for flag in ENABLE_OPEN_WIFI_FALLBACK ENABLE_HTTP_UA_REWRITE ENABLE_TOR_TRANSPARENT ENABLE_BLOCKLISTS ENABLE_DOT ENABLE_VPN_KILLSWITCH ENABLE_AUTO_UPDATES ENABLE_AVAHI_REFLECTOR ENABLE_ADGUARD ENABLE_AP_SCHEDULE ENABLE_CLIENT_QOS ENABLE_PER_DEVICE_VPN ENABLE_CAKE_AUTOTUNE ENABLE_SPLIT_TUNNEL ENABLE_2FA ENABLE_WAN_METRICS ENABLE_BANDWIDTH_DASHBOARD ENABLE_PROMETHEUS_EXPORTER ENABLE_UPS_MONITOR ENABLE_WIREGUARD; do
    _safe_write_conf "$flag" "${!flag:-0}" "$DEFAULTS_FILE"
done

# Write string values safely (C11/H22)
_safe_write_conf "NTFY_TOPIC"            "${NTFY_TOPIC:-}"               "$DEFAULTS_FILE"
_safe_write_conf "HEADSCALE_URL"         "${HEADSCALE_URL:-}"            "$DEFAULTS_FILE"
_safe_write_conf "SPLIT_TUNNEL_DOMAINS"  "${SPLIT_TUNNEL_DOMAINS:-}"     "$DEFAULTS_FILE"
_safe_write_conf "IPHONE_BT_MAC"         "${IPHONE_BT_MAC:-}"            "$DEFAULTS_FILE"
_safe_write_conf "SSH_ADMIN_KEY"         "${SSH_ADMIN_KEY:-}"            "$DEFAULTS_FILE"
_safe_write_conf "AP_CLIENT_BANDWIDTH"   "${AP_CLIENT_BANDWIDTH:-unlimited}" "$DEFAULTS_FILE"
_safe_write_conf "AP_DISABLE_TIME"       "${AP_DISABLE_TIME:-02:00}"     "$DEFAULTS_FILE"
_safe_write_conf "AP_ENABLE_TIME"        "${AP_ENABLE_TIME:-07:00}"      "$DEFAULTS_FILE"
_safe_write_conf "VPN_DEVICE_MACS"       "${VPN_DEVICE_MACS:-}"          "$DEFAULTS_FILE"
# TOR_AP_PASS: store empty placeholder to avoid writing plaintext passphrase
_safe_write_conf "TOR_AP_PASS"           ""                              "$DEFAULTS_FILE" 2>/dev/null || true
_safe_write_conf "PUSHGW_URL"              "${PUSHGW_URL:-}"                    "$DEFAULTS_FILE"
_safe_write_conf "UPS_SHUTDOWN_THRESHOLD"  "${UPS_SHUTDOWN_THRESHOLD:-10}"      "$DEFAULTS_FILE"
_safe_write_conf "TAILSCALE_UP_ARGS"       "${TAILSCALE_UP_ARGS:-}"             "$DEFAULTS_FILE"
_safe_write_conf "WG_LISTEN_PORT"          "${WG_LISTEN_PORT:-51820}"           "$DEFAULTS_FILE"
_safe_write_conf "WG_PEER_PUBKEY"          "${WG_PEER_PUBKEY:-}"                "$DEFAULTS_FILE"
_safe_write_conf "WG_PEER_ENDPOINT"        "${WG_PEER_ENDPOINT:-}"              "$DEFAULTS_FILE"
_safe_write_conf "WG_PEER_ALLOWED_IPS"     "${WG_PEER_ALLOWED_IPS:-}"           "$DEFAULTS_FILE"

ok "/etc/default/travel-router written"

# ── 13. Systemd units ─────────────────────────────────────────────────────────
section "Systemd units"

SYSTEMD_DEST="/etc/systemd/system"
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
    tune-cake.service tune-cake.timer \
    ota-commit.service ota-commit.timer; do
    install_file "systemd/$unit" "$SYSTEMD_DEST/$unit" 644
    ok "  $unit"
done

# OTA scripts
install -m 0755 "${REPO}/scripts/ota-update.sh"   /usr/local/sbin/ota-update
install -m 0755 "${REPO}/scripts/ota-commit.sh"   /usr/local/sbin/ota-commit
install -m 0755 "${REPO}/scripts/ota-rollback.sh" /usr/local/sbin/ota-rollback
ok "  ota-update / ota-commit / ota-rollback → /usr/local/sbin/"

systemctl daemon-reload

for unit in \
    failover-watchdog.timer wan-watchdog.timer \
    cpu-performance.service cake-qdisc.service \
    wlan-mac-random.service \
    vnstat-metrics.timer update-blocklists.timer \
    tailscale-watchdog.timer \
    wireguard-watchdog.timer \
    daily-digest.timer \
    update-router.timer \
    ota-commit.timer; do
    if systemctl enable "$unit" 2>/dev/null; then ok "  enabled: $unit"; else warn "  could not enable $unit"; fi
done

# Trigger initial blocklist load immediately if enabled (daily timer fires first time at next scheduled slot)
if [[ "${ENABLE_BLOCKLISTS:-0}" = "1" ]]; then
    info "Running initial blocklist load (this may take ~30s)..."
    if systemctl start update-blocklists.service 2>/dev/null; then
        ok "Initial blocklist loaded"
    else
        warn "Initial blocklist load failed — will retry at next timer fire"
        warn "  Check: journalctl -u update-blocklists.service -n 20"
    fi
fi

# ── 14. udev rules ────────────────────────────────────────────────────────────
section "udev rules"

install_file config/90-ipheth.rules /etc/udev/rules.d/90-ipheth.rules 644
install_file config/99-apple-autosuspend.rules /etc/udev/rules.d/99-apple-autosuspend.rules 644
install_file config/91-android-tether.rules /etc/udev/rules.d/91-android-tether.rules 644
install_file config/modules-android-tether.conf /etc/modules-load.d/android-tether.conf 644
udevadm control --reload-rules 2>/dev/null || true
ok "udev rules installed (ipheth + Android tethering + USB autosuspend)"

# ── 15. log2ram ───────────────────────────────────────────────────────────────
section "log2ram"

if [[ -f /etc/log2ram.conf ]]; then
    sed -i 's/^SIZE=.*/SIZE=128M/' /etc/log2ram.conf
    # L12: idempotent JOURNALD_AWARE update
    python3 -c "
import sys, re
path = sys.argv[1]
with open(path) as f: content = f.read()
if 'JOURNALD_AWARE' not in content:
    content += '\nJOURNALD_AWARE=true\n'
else:
    content = re.sub(r'JOURNALD_AWARE=\w+', 'JOURNALD_AWARE=true', content)
with open(path, 'w') as f: f.write(content)
" /etc/log2ram.conf
    ok "log2ram: SIZE=128M, JOURNALD_AWARE=true"
fi

# ── 16. privoxy — optional User-Agent normalization ──────────────────────────
section "privoxy — optional HTTP User-Agent normalization"

install_file config/privoxy-user.action /etc/privoxy/user.action 644
if [[ "${ENABLE_HTTP_UA_REWRITE:-0}" = "1" ]]; then
    systemctl enable --now privoxy 2>/dev/null || true
    ok "privoxy configured and enabled"
else
    systemctl disable --now privoxy 2>/dev/null || true
    ok "privoxy installed but disabled by default"
fi

# ── 17. Tor — optional transparent proxy ─────────────────────────────────────
section "Tor — optional transparent proxy config"

# Append transparent proxy config if not already present
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
else
    ok "Tor transparent proxy disabled by default"
fi

if [[ "${ENABLE_TOR_TRANSPARENT:-0}" = "1" ]]; then
    systemctl enable tor 2>/dev/null || true
    ok "Tor enabled"

    # Test whether brcmfmac supports a second virtual AP for a dedicated Tor SSID
    if iw dev wlan0 interface add uap1 type __ap 2>/dev/null; then
        iw dev uap1 del 2>/dev/null || true

        # C7: TOR_AP_PASS must be set (guarded earlier), no fallback to 'changeme'
        [[ -n "$TOR_AP_PASS" ]] || die "TOR_AP_PASS is empty"
        # Second BSS in hostapd.conf (uses same radio, separate SSID)
        if ! grep -q "^bss=uap1" /etc/hostapd/hostapd.conf; then
            cat >> /etc/hostapd/hostapd.conf << 'TOREOF'

# Tor transparent-proxy AP (all traffic routed through Tor)
bss=uap1
ssid=TorAP
wpa=2
wpa_passphrase=PLACEHOLDER_TOR_PASS
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
TOREOF
            # C6/C7: write Tor passphrase safely via Python (atomic mktemp+replace)
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
" "$TOR_AP_PASS"
        fi

        install_file config/dnsmasq-tor-ap.conf /etc/dnsmasq.d/tor-ap.conf

        # I-H7: write uap1 creation to a drop-in instead of sed-patching rc.local
        mkdir -p /etc/rc.local.d
        cat > /etc/rc.local.d/50-tor-uap1.sh << 'RCEOF'
#!/bin/sh
# Created by pi-travel-router install.sh — I-H7
# Create second virtual AP for Tor transparent proxy SSID
iw dev wlan0 interface add uap1 type __ap || true
RCEOF
        chmod 755 /etc/rc.local.d/50-tor-uap1.sh

        # Ensure rc.local sources drop-ins (idempotent)
        if [[ -f /etc/rc.local ]] && ! grep -q "rc.local.d" /etc/rc.local; then
            # shellcheck disable=SC2016  # single quotes intentional in sed script
            sed -i '/^exit 0/i # Source rc.local drop-ins\nfor _f in /etc/rc.local.d/*.sh; do [ -f "$_f" ] \&\& . "$_f"; done' /etc/rc.local
        fi

        ok "uap1 supported — TorAP SSID configured on 172.16.100.0/24"
    else
        warn "uap1 not supported by brcmfmac — Tor AP uses static-IP fallback"
        warn "  Clients: set static IP 172.16.100.x/24, GW+DNS 172.16.100.1 on the main AP"
    fi
else
    systemctl disable --now tor 2>/dev/null || true
    # I-H7: remove uap1 drop-in when Tor is disabled
    rm -f /etc/rc.local.d/50-tor-uap1.sh
    ok "Tor disabled by default"
fi

# ── 18. Tailscale ─────────────────────────────────────────────────────────────
section "Tailscale"

systemctl enable --now tailscaled 2>/dev/null || true

if [[ -n "$TS_KEY" ]]; then
    # I-M1: use read -ra to avoid word-split issues with IFS-modified environments
    read -ra TS_ARGS <<< "${TAILSCALE_UP_ARGS:-}"
    # Validate TAILSCALE_UP_ARGS against forbidden flags
    _FORBIDDEN_TS=("--authkey" "--reset" "--force-reauth" "--auth-key")
    for _targ in "${TS_ARGS[@]}"; do
        for _f in "${_FORBIDDEN_TS[@]}"; do
            [[ "$_targ" = "$_f" || "$_targ" = "${_f}="* ]] && \
                die "TAILSCALE_UP_ARGS contains forbidden flag: $_targ"
        done
    done
    _TS_LOGIN_ARGS=()
    [[ -n "$HEADSCALE_URL" ]] && _TS_LOGIN_ARGS+=(--login-server="$HEADSCALE_URL")
    if tailscale up \
        --authkey="$TS_KEY" \
        "${_TS_LOGIN_ARGS[@]}" \
        "${TS_ARGS[@]}" \
        2>/dev/null; then
        ok "Tailscale authenticated and subnet advertised"
    else
        warn "Tailscale auth failed — run manually: sudo tailscale up $TAILSCALE_UP_ARGS"
    fi
else
    warn "No Tailscale key provided. After reboot, run:"
    if [[ -n "$HEADSCALE_URL" ]]; then
        warn "  sudo tailscale up --login-server=\"$HEADSCALE_URL\" $TAILSCALE_UP_ARGS"
    else
        warn "  sudo tailscale up $TAILSCALE_UP_ARGS"
    fi
fi

# ── 18b. WireGuard ───────────────────────────────────────────────────────────
section "WireGuard"

ENABLE_WIREGUARD="${ENABLE_WIREGUARD:-0}"
if [[ "$ENABLE_WIREGUARD" = "1" ]]; then
    mkdir -p /etc/wireguard
    chmod 700 /etc/wireguard
    if [[ ! -f /etc/wireguard/wg0.key ]]; then
        wg genkey | tee /etc/wireguard/wg0.key | wg pubkey > /etc/wireguard/wg0.pub
        chmod 600 /etc/wireguard/wg0.key
    fi
    # Derive the first host address from WG_NETWORK
    _wg_server_addr=$(python3 -c "
import ipaddress, sys
n = ipaddress.ip_network(sys.argv[1], strict=False)
print(str(list(n.hosts())[0]))
" "${WG_NETWORK:-10.9.0.0/24}")
    # Substitute template placeholders via Python (handles special chars safely)
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
" "$REPO/config/wg0.conf.template" /etc/wireguard/wg0.conf \
      "$(cat /etc/wireguard/wg0.key)" \
      "$_wg_server_addr" \
      "${WG_LISTEN_PORT:-51820}"
    # Append [Peer] section if a peer public key was supplied
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
    systemctl enable wg-quick@wg0 2>/dev/null || true
    ok "WireGuard configured (wg0); public key: $(cat /etc/wireguard/wg0.pub 2>/dev/null || echo 'unknown')"
else
    systemctl disable wg-quick@wg0 2>/dev/null || true
    ok "WireGuard disabled (set ENABLE_WIREGUARD=1 to activate)"
fi

# ── 19. firewall rules ───────────────────────────────────────────────────────
# I-H4: firewall applied AFTER tailscaled is enabled/started so tailscale0
#        interface exists when iptables rules that reference it are saved.
section "Firewall — TTL, DSCP, isolation, optional proxy rules"

/usr/local/bin/travel-router-firewall.sh --save
ok "Firewall rules applied and saved"

# ── 20. usbmuxd / ipheth ──────────────────────────────────────────────────────
section "usbmuxd hardening"

# Ensure usbmuxd restarts on failure (iOS 18 CPU spin bug workaround)
mkdir -p /etc/systemd/system/usbmuxd.service.d
cat > /etc/systemd/system/usbmuxd.service.d/restart.conf << 'EOF'
[Service]
Restart=on-failure
RestartSec=3
CPUQuota=20%
EOF
systemctl daemon-reload
ok "usbmuxd: Restart=on-failure, CPUQuota=20%"

# ── 21. Enable usbmuxd + bluetooth ───────────────────────────────────────────
systemctl enable usbmuxd  2>/dev/null || true
systemctl enable bluetooth 2>/dev/null || true

# ── 22. vnStat interface init ────────────────────────────────────────────────
section "vnStat"
mkdir -p /var/lib/prometheus/node-exporter
vnstat --add -i wlan0 2>/dev/null || true
vnstat --add -i uap0  2>/dev/null || true
ok "vnStat tracking wlan0 + uap0"

# ── 23. Unattended security updates ──────────────────────────────────────────
section "Unattended security updates"

if [[ "${ENABLE_AUTO_UPDATES:-0}" = "1" ]]; then
    install_file config/50unattended-upgrades          /etc/apt/apt.conf.d/50unattended-upgrades 644
    install_file config/20auto-upgrades                /etc/apt/apt.conf.d/20auto-upgrades 644
    install_file config/99-travel-router-notify.conf   /etc/apt/apt.conf.d/99-travel-router-notify 644
    systemctl enable --now unattended-upgrades 2>/dev/null || true
    ok "Auto security updates enabled (reboot at 03:30 when required)"
else
    ok "Auto security updates disabled (set ENABLE_AUTO_UPDATES=1 to activate)"
fi

# ── §. nftables TTL / DSCP / hop-limit migration ─────────────────────────────
section "nftables TTL/DSCP rules"

mkdir -p /etc/nftables.conf.d
install_file config/nftables-travel-router.nft /etc/nftables.conf.d/travel-router.nft 644

# Add include directive to /etc/nftables.conf if not already present
if ! grep -q "nftables.conf.d" /etc/nftables.conf 2>/dev/null; then
    printf '\ninclude "/etc/nftables.conf.d/*.nft"\n' >> /etc/nftables.conf
fi

nft -f /etc/nftables.conf.d/travel-router.nft 2>/dev/null || \
    warn "nft load failed — rules will apply on next nftables service start"
systemctl enable nftables 2>/dev/null || true
ok "nftables TTL/DSCP rules loaded (replaces iptables mangle for TTL + DSCP)"

# ── §. Domain-based split tunnel (#45) ───────────────────────────────────────
section "Domain-based split tunnel"

install_file scripts/apply-split-tunnel.sh /usr/local/bin/apply-split-tunnel.sh 755

SYSTEMD_DEST_ST="/etc/systemd/system"
install_file systemd/split-tunnel.service "$SYSTEMD_DEST_ST/split-tunnel.service" 644
systemctl daemon-reload

if [[ "${ENABLE_SPLIT_TUNNEL:-0}" = "1" ]]; then
    if [[ -z "${SPLIT_TUNNEL_DOMAINS:-}" ]]; then
        warn "ENABLE_SPLIT_TUNNEL=1 but SPLIT_TUNNEL_DOMAINS is empty"
        warn "  Set SPLIT_TUNNEL_DOMAINS in /etc/default/travel-router and run:"
        warn "  sudo systemctl restart dnsmasq split-tunnel.service"
    else
        apt-get install -y ipset 2>/dev/null || true
        _DOMAIN_PATH=$(printf '%s' "$SPLIT_TUNNEL_DOMAINS" | tr ' ' '/')
        printf "# Split tunnel domains — generated by install.sh\nipset=/%s/vpn_domains\n" \
            "$_DOMAIN_PATH" > /etc/dnsmasq.d/split-tunnel.conf
        systemctl enable --now split-tunnel.service 2>/dev/null || true
        systemctl restart dnsmasq 2>/dev/null || true
        ok "Split tunnel enabled — domains via Tailscale: ${SPLIT_TUNNEL_DOMAINS}"
    fi
else
    systemctl disable split-tunnel.service 2>/dev/null || true
    rm -f /etc/dnsmasq.d/split-tunnel.conf
    ok "Split tunnel disabled (set ENABLE_SPLIT_TUNNEL=1 + SPLIT_TUNNEL_DOMAINS)"
fi

# ── §. CAKE bandwidth auto-tuning ─────────────────────────────────────────────
section "CAKE bandwidth auto-tuning"

install_file scripts/tune-cake.sh /usr/local/bin/tune-cake.sh 755

if [[ "${ENABLE_CAKE_AUTOTUNE:-0}" = "1" ]]; then
    apt-get install -y speedtest-cli 2>/dev/null || true
    systemctl enable --now tune-cake.timer 2>/dev/null || true
    ok "CAKE auto-tune enabled — weekly speedtest adjusts wlan0 CAKE bandwidth"
    ok "Run manually: sudo tune-cake.sh"
else
    systemctl disable tune-cake.timer 2>/dev/null || true
    ok "CAKE auto-tune disabled (set ENABLE_CAKE_AUTOTUNE=1 to activate)"
fi

# ── §. SSH two-factor authentication (#19) ───────────────────────────────────
section "SSH 2FA (TOTP)"

install_file scripts/setup-2fa.sh /usr/local/bin/setup-2fa.sh 755

if [[ "${ENABLE_2FA:-0}" = "1" ]]; then
    apt-get install -y libpam-google-authenticator 2>/dev/null || true
    install_file config/sshd-2fa.conf /etc/ssh/sshd_config.d/98-travel-router-2fa.conf 644
    # Add pam_google_authenticator to sshd PAM if not already present
    if ! grep -q "pam_google_authenticator" /etc/pam.d/sshd 2>/dev/null; then
        printf "auth required pam_google_authenticator.so nullok\n" >> /etc/pam.d/sshd
    fi
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
    ok "SSH 2FA enabled — run: sudo -u \$(logname) setup-2fa.sh to configure TOTP"
    # I-M3: warn for any login-shell users who have not yet configured TOTP
    while IFS=: read -r _u _ _uid _ _ _home _shell; do
        [[ "$_uid" -lt 1000 && "$_u" != "root" ]] && continue
        [[ "$_shell" == */false || "$_shell" == */nologin ]] && continue
        [[ -f "$_home/.google_authenticator" ]] && continue
        warn "2FA not configured for user $_u — run: sudo -u $_u setup-2fa.sh"
    done < /etc/passwd
else
    ok "SSH 2FA disabled (set ENABLE_2FA=1 then run setup-2fa.sh)"
fi

# ── §. WAN metric auto-management (#27) ─────────────────────────────────────
section "WAN metric auto-management"

install_file config/nm-wan-metrics /etc/NetworkManager/dispatcher.d/50-wan-metrics 755

if [[ "${ENABLE_WAN_METRICS:-1}" = "1" ]]; then
    ok "WAN metric dispatcher installed — enforces enx*=100 rndis0=200 bnep0=300 wlan0=600"
else
    rm -f /etc/NetworkManager/dispatcher.d/50-wan-metrics
    ok "WAN metric dispatcher disabled"
fi

# ── §. IPv6 — DHCPv6 uplink + SLAAC on AP ───────────────────────────────────
section "IPv6 — DHCPv6 uplink + SLAAC on AP (radvd)"

# Install DHCPv6 client config
install_file config/dhclient6.conf /etc/dhclient6.conf 644

# Install NM dispatcher script to start DHCPv6 on WAN interfaces
mkdir -p /etc/NetworkManager/dispatcher.d
install_file config/nm-dispatcher/70-dhcpv6-uplink.sh \
    /etc/NetworkManager/dispatcher.d/70-dhcpv6-uplink 755
ok "DHCPv6 uplink dispatcher installed → /etc/NetworkManager/dispatcher.d/70-dhcpv6-uplink"

# Install radvd config for SLAAC on uap0 AP interface
if command -v radvd >/dev/null 2>&1; then
    install_file config/radvd.conf /etc/radvd.conf 644
    systemctl enable radvd 2>/dev/null || true
    ok "radvd: Router Advertisement config installed for uap0 — AP clients get SLAAC IPv6"
else
    warn "radvd not found — SLAAC/RA on uap0 will not work (install radvd manually)"
fi

# Selectively re-enable IPv6 on the AP interface (uap0) while keeping uplinks disabled.
# The 99-disable-ipv6-uplink.conf sysctl disables IPv6 on wlan0/eth0/default.
# uap0 needs IPv6 enabled so radvd can send Router Advertisements.
if ! grep -q "uap0" /etc/sysctl.d/99-disable-ipv6-uplink.conf 2>/dev/null; then
    cat >> /etc/sysctl.d/99-disable-ipv6-uplink.conf << 'EOF'

# uap0: keep IPv6 enabled so radvd can send Router Advertisements to AP clients
net.ipv6.conf.uap0.disable_ipv6 = 0
EOF
    sysctl -p /etc/sysctl.d/99-disable-ipv6-uplink.conf &>/dev/null || true
    ok "sysctl: IPv6 enabled on uap0 (needed for radvd RA)"
fi

# ── §. WiFi QR code ───────────────────────────────────────────────────────────
section "WiFi QR code"

WIFI_QR_DIR="/usr/local/share/travel-router/wifi-qr"
mkdir -p "$WIFI_QR_DIR"

# Validate AP_SSID and AP_PASS for shell-unsafe characters before QR assembly.
# Backticks, $(), and backslash-escapes can cause injection in shell-interpolated strings.
# shellcheck disable=SC2016,SC1003  # character class uses single quotes intentionally
if [[ "$AP_SSID" =~ ['`$()\\'] ]] || [[ "$AP_PASS" =~ ['`$()\\'] ]]; then
    warn "AP_SSID or AP_PASS contains shell-unsafe characters; skipping QR code generation"
else
    # Assemble QR string entirely in Python to avoid shell metacharacter interpretation.
    WIFI_STRING=$(python3 -c "
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
    printf '%s\n' "$WIFI_STRING" > "$WIFI_QR_DIR/wifi-string.txt"
    chmod 600 "$WIFI_QR_DIR/wifi-string.txt"

    if command -v qrencode >/dev/null 2>&1; then
        qrencode -t UTF8 -o "$WIFI_QR_DIR/wifi-qr.txt" "$WIFI_STRING"
        chmod 600 "$WIFI_QR_DIR/wifi-qr.txt"
        ok "WiFi QR code saved to $WIFI_QR_DIR/wifi-qr.txt"
        ok "Display with: cat $WIFI_QR_DIR/wifi-qr.txt"
        echo ""
        # I-M2: redirect to /dev/tty so passphrase is not captured in the install log
        qrencode -t UTF8 "$WIFI_STRING" > /dev/tty || true
        echo ""
    else
        ok "qrencode not available — WiFi string saved to $WIFI_QR_DIR/wifi-string.txt"
    fi
fi

# ── §. Monitoring — Prometheus node exporter (#33) ───────────────────────────
section "Prometheus node exporter"

if [[ "${ENABLE_PROMETHEUS_EXPORTER:-0}" = "1" ]]; then
    apt-get install -y prometheus-node-exporter 2>/dev/null || true
    systemctl enable --now prometheus-node-exporter 2>/dev/null || true
    ok "Prometheus node exporter enabled on :9100 (accessible via Tailscale)"
    ok "Scrape with: http://$(tailscale ip -4 2>/dev/null | head -1):9100/metrics"
else
    ok "Prometheus node exporter disabled (set ENABLE_PROMETHEUS_EXPORTER=1 to activate)"
fi

# ── §. Bandwidth analytics dashboard (#32) ───────────────────────────────────
section "Bandwidth analytics dashboard"

install_file scripts/generate-bandwidth-report.sh \
    /usr/local/bin/generate-bandwidth-report.sh 755
install_file systemd/generate-bandwidth-report.service \
    "/etc/systemd/system/generate-bandwidth-report.service" 644
install_file systemd/generate-bandwidth-report.timer \
    "/etc/systemd/system/generate-bandwidth-report.timer" 644
install_file systemd/vnstat-push.service \
    "/etc/systemd/system/vnstat-push.service" 644
install_file systemd/vnstat-push.timer \
    "/etc/systemd/system/vnstat-push.timer" 644
install_file scripts/vnstat-push.sh /usr/local/bin/vnstat-push.sh 755
systemctl daemon-reload

if [[ "${ENABLE_BANDWIDTH_DASHBOARD:-0}" = "1" ]]; then
    systemctl enable --now generate-bandwidth-report.timer 2>/dev/null || true
    /usr/local/bin/generate-bandwidth-report.sh 2>/dev/null || true
    # Serve via lighttpd: symlink into webroot
    ln -sf /var/lib/travel-router/bandwidth.html \
        /var/www/html/bandwidth.html 2>/dev/null || true
    ok "Bandwidth dashboard: http://${AP_GATEWAY}/bandwidth.html"
    ok "Regenerated daily at 00:05"
else
    systemctl disable generate-bandwidth-report.timer 2>/dev/null || true
    ok "Bandwidth dashboard disabled (set ENABLE_BANDWIDTH_DASHBOARD=1)"
fi

# Install bmon + iftop for real-time traffic inspection (#34)
apt-get install -y bmon iftop 2>/dev/null || true
ok "Real-time traffic: bmon (interfaces) and iftop (per-connection) installed"

# Enable vnStat push if PUSHGW_URL configured
if [[ -n "${PUSHGW_URL:-}" ]]; then
    systemctl enable --now vnstat-push.timer 2>/dev/null || true
    ok "vnStat Prometheus push enabled (hourly) → $PUSHGW_URL"
else
    ok "vnStat push disabled (set PUSHGW_URL in /etc/default/travel-router)"
fi

# ── §. Avahi — mDNS reflector ────────────────────────────────────────────────
section "Avahi — mDNS reflector"

install_file config/avahi-daemon.conf /etc/avahi/avahi-daemon.conf 644

if [[ "${ENABLE_AVAHI_REFLECTOR:-0}" = "1" ]]; then
    systemctl enable --now avahi-daemon 2>/dev/null || true
    ok "Avahi mDNS reflector enabled (uap0 ↔ tailscale0)"
else
    systemctl disable --now avahi-daemon 2>/dev/null || true
    ok "Avahi installed but disabled (set ENABLE_AVAHI_REFLECTOR=1 to activate)"
fi

# ── §. UPS / PiSugar battery monitor (#50) ───────────────────────────────────
section "UPS battery monitor (PiSugar 3)"

install_file scripts/ups-monitor.sh /usr/local/bin/ups-monitor.sh 755
install_file systemd/ups-monitor.service "/etc/systemd/system/ups-monitor.service" 644
install_file systemd/ups-monitor.timer "/etc/systemd/system/ups-monitor.timer" 644
systemctl daemon-reload

if [[ "${ENABLE_UPS_MONITOR:-0}" = "1" ]]; then
    systemctl enable --now ups-monitor.timer 2>/dev/null || true
    ok "UPS monitor enabled — battery checked every 5 min, shutdown at ${UPS_SHUTDOWN_THRESHOLD:-10}%"
    ok "PiSugar server (optional): https://github.com/PiSugar/pisugar-power-manager-rs"
else
    systemctl disable ups-monitor.timer 2>/dev/null || true
    ok "UPS monitor disabled (set ENABLE_UPS_MONITOR=1 to activate)"
    ok "Requires: PiSugar 3 HAT — https://www.pisugar.com"
fi

# ── §. Scheduled AP disable (#29) ────────────────────────────────────────────
section "Scheduled AP disable"

if [[ "${ENABLE_AP_SCHEDULE:-0}" = "1" ]]; then
    # H20: write drop-in overrides with user-supplied times
    mkdir -p /etc/systemd/system/ap-disable.timer.d
    cat > /etc/systemd/system/ap-disable.timer.d/time.conf << EOF
[Timer]
OnCalendar=
OnCalendar=*-*-* ${AP_DISABLE_TIME:-02:00}:00
EOF
    mkdir -p /etc/systemd/system/ap-enable.timer.d
    cat > /etc/systemd/system/ap-enable.timer.d/time.conf << EOF
[Timer]
OnCalendar=
OnCalendar=*-*-* ${AP_ENABLE_TIME:-07:00}:00
EOF
    systemctl daemon-reload
    systemctl enable ap-disable.timer ap-enable.timer 2>/dev/null || true
    ok "AP schedule enabled: disable at ${AP_DISABLE_TIME:-02:00}, re-enable at ${AP_ENABLE_TIME:-07:00}"
else
    systemctl disable ap-disable.timer ap-enable.timer 2>/dev/null || true
    ok "AP schedule disabled (set ENABLE_AP_SCHEDULE=1 to activate)"
fi

# ── §. AdGuard Home — DNS ad-blocker ─────────────────────────────────────────
section "AdGuard Home — DNS ad-blocker"

if [[ "${ENABLE_ADGUARD:-0}" = "1" ]]; then
    info "Downloading AdGuard Home binary..."
    # I-M4: handle install-adguard.sh failures gracefully
    if ! bash "$REPO/scripts/install-adguard.sh"; then
        warn "AdGuard Home installation failed — check network and retry"
        warn "To retry: bash /opt/pi-travel-router/scripts/install-adguard.sh"
        ENABLE_ADGUARD=0
    fi
    install_file config/AdGuardHome.yaml /opt/AdGuardHome/AdGuardHome.yaml 640
    install_file config/dnsmasq-adguard.conf /etc/dnsmasq.d/adguard.conf
    rm -f /etc/dnsmasq.d/dot.conf
    systemctl enable --now adguard-home 2>/dev/null || true
    ok "AdGuard Home enabled — web UI at http://${AP_GATEWAY}:3000 (set password on first visit)"
    ok "DNS: dnsmasq → AdGuard Home (127.0.0.1:5335) → DoT upstreams"
else
    ok "AdGuard Home disabled (set ENABLE_ADGUARD=1 to activate)"
fi

# ── §. Web management dashboard ───────────────────────────────────────────────
install_web_dashboard() {
    log "Installing web dashboard..."
    # Generate auth token if not already present
    WEB_TOKEN_FILE="/var/lib/travel-router/web-token"
    mkdir -p /var/lib/travel-router
    if [ ! -f "${WEB_TOKEN_FILE}" ]; then
        python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "${WEB_TOKEN_FILE}"
        chmod 0600 "${WEB_TOKEN_FILE}"
    fi
    cp -r "${REPO}/web" /opt/pi-travel-router/
    install_file "${REPO}/systemd/travel-router-web.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable travel-router-web.service
    log "Web dashboard installed — token at ${WEB_TOKEN_FILE}"
}

section "Web management dashboard"
install_web_dashboard
ok "Web dashboard enabled on :8080 — token at /var/lib/travel-router/web-token"

# ── §. Hardware watchdog ──────────────────────────────────────────────────────
section "Hardware watchdog (BCM2835)"

# Tell systemd to kick the hardware watchdog every 15s; reboot if it misses.
if ! grep -q "RuntimeWatchdogSec" /etc/systemd/system.conf 2>/dev/null; then
    cat >> /etc/systemd/system.conf << 'EOF'

# Hardware watchdog — reboots Pi if kernel locks up
RuntimeWatchdogSec=15
ShutdownWatchdogSec=5min
EOF
fi
ok "Hardware watchdog enabled (RuntimeWatchdogSec=15 — needs reboot to activate)"

# ── §. Log rotation ───────────────────────────────────────────────────────────
section "Log rotation"

cat > /etc/logrotate.d/travel-router << 'EOF'
/var/log/wan-watchdog.log /var/log/travel-router*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
EOF
ok "Log rotation configured (daily, 7-day retention, compressed)"

# ── §. Per-client bandwidth fairness (#21) ───────────────────────────────────
section "Per-client bandwidth fairness (CAKE per-host)"

if [[ "${ENABLE_CLIENT_QOS:-0}" = "1" ]]; then
    # H21: actually apply CAKE and create a boot service
    _safe_write_conf "AP_CLIENT_BANDWIDTH" "${AP_CLIENT_BANDWIDTH:-unlimited}" "$DEFAULTS_FILE"
    if [[ -x /usr/local/bin/apply-cake.sh ]]; then
        /usr/local/bin/apply-cake.sh 2>&1 | tee -a "$LOG" || warn "CAKE apply failed (will retry at boot)"
    fi
    cat > /etc/systemd/system/apply-cake.service << 'EOF'
[Unit]
Description=Apply CAKE qdisc on AP interface
After=hostapd.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/apply-cake.sh
[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable apply-cake.service 2>/dev/null || true
    ok "CAKE per-host enabled on uap0 (cap: ${AP_CLIENT_BANDWIDTH:-unlimited})"
    ok "apply-cake.service enabled for boot-time activation"
else
    systemctl disable apply-cake.service 2>/dev/null || true
    ok "Per-client QoS disabled (set ENABLE_CLIENT_QOS=1 to activate)"
fi

# ── §. Per-device Tailscale routing (#44) ────────────────────────────────────
section "Per-device Tailscale routing"

if [[ "${ENABLE_PER_DEVICE_VPN:-0}" = "1" ]]; then
    if [[ -z "${VPN_DEVICE_MACS:-}" ]]; then
        warn "ENABLE_PER_DEVICE_VPN=1 but VPN_DEVICE_MACS is empty"
        warn "  Add MACs to VPN_DEVICE_MACS in /etc/default/travel-router"
    else
        ok "Per-device VPN routing enabled for: ${VPN_DEVICE_MACS}"
    fi
else
    ok "Per-device VPN routing disabled (set ENABLE_PER_DEVICE_VPN=1 + VPN_DEVICE_MACS)"
fi

# ── §. Daily digest notification ─────────────────────────────────────────────
section "Daily digest notification"

if [[ -n "${NTFY_TOPIC:-}" ]]; then
    ok "Daily digest enabled — 08:00 ntfy push with uptime, uplink, Tailscale state"
else
    ok "Daily digest installed — set NTFY_TOPIC in /etc/default/travel-router to activate"
fi

# ── §. SSH hardening ─────────────────────────────────────────────────────────
section "SSH hardening"

install_file config/sshd-travel-router.conf /etc/ssh/sshd_config.d/99-travel-router.conf 644

ADMIN_USER="${SUDO_USER:-}"
if [[ -z "$ADMIN_USER" ]]; then
    ADMIN_USER=$(logname 2>/dev/null || echo "${USER:-root}")
fi
ADMIN_HOME=$(getent passwd "$ADMIN_USER" 2>/dev/null | cut -d: -f6)
ADMIN_HOME="${ADMIN_HOME:-/root}"

if [[ -n "${SSH_ADMIN_KEY:-}" ]]; then
    # Strip embedded newlines that could inject extra lines into authorized_keys
    SSH_ADMIN_KEY="$(printf '%s' "$SSH_ADMIN_KEY" | tr -d '\n\r')"
    [[ "$SSH_ADMIN_KEY" =~ ^(ssh-|ecdsa-|sk-) ]] || die "Invalid SSH key format: SSH_ADMIN_KEY must start with ssh-, ecdsa-, or sk-"
    mkdir -p "$ADMIN_HOME/.ssh"
    chmod 700 "$ADMIN_HOME/.ssh"
    touch "$ADMIN_HOME/.ssh/authorized_keys"
    chmod 600 "$ADMIN_HOME/.ssh/authorized_keys"
    if ! grep -qF "$SSH_ADMIN_KEY" "$ADMIN_HOME/.ssh/authorized_keys" 2>/dev/null; then
        printf '%s\n' "$SSH_ADMIN_KEY" >> "$ADMIN_HOME/.ssh/authorized_keys"
    fi
    chown -R "$ADMIN_USER:$ADMIN_USER" "$ADMIN_HOME/.ssh"
    grep -q "PasswordAuthentication" /etc/ssh/sshd_config.d/99-travel-router.conf 2>/dev/null || \
        echo "PasswordAuthentication no" >> /etc/ssh/sshd_config.d/99-travel-router.conf
    ok "SSH public key added for $ADMIN_USER ($ADMIN_HOME); password auth disabled"
else
    ok "No SSH key provided — password auth remains enabled"
    ok "Add later: echo '<pubkey>' >> ~/.ssh/authorized_keys"
fi

systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
ok "sshd restarted with hardened config"

# ── §. MOTD + status command ─────────────────────────────────────────────────
section "MOTD + status command"

install_file config/motd-travel-router /etc/update-motd.d/10-travel-router 755
chmod +x /etc/update-motd.d/10-travel-router
ok "SSH login MOTD installed (calls travel-status.sh)"
ok "Run 'sudo travel-tui' for the interactive dashboard"

# ── 24. Version stamp ─────────────────────────────────────────────────────────
section "Version stamp"
INSTALLED_VERSION="$(cat "$REPO/VERSION" 2>/dev/null || echo "unknown")"
echo "$INSTALLED_VERSION" > /etc/travel-router-version
# Keep a copy of install.sh for reference (update-router.sh compares against it)
mkdir -p /usr/local/share/travel-router
cp "$REPO/install.sh" /usr/local/share/travel-router/install.sh
chmod 755 /usr/local/share/travel-router/install.sh
ok "Installed version: $INSTALLED_VERSION"

# ── Done ──────────────────────────────────────────────────────────────────────
section "Installation complete"

echo ""
echo -e "  ${G}${BLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "  ${G}${BLD}║   ✓  Installation complete!                          ║${NC}"
echo -e "  ${G}${BLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BLD}Next steps${NC}"
echo ""
echo -e "  ${BLD}1.${NC}  Reboot the Pi:"
echo    "        sudo reboot"
echo ""
echo -e "  ${BLD}2.${NC}  After reboot, connect your laptop:"
echo    "        Plug the Pi's middle USB port into your laptop"
echo    "        → USB Ethernet adapter appears automatically"
echo    "        → Web UI:  http://192.168.7.1"
echo    "        → SSH:     ssh root@192.168.7.1"
echo ""
echo -e "  ${BLD}3.${NC}  Connect your devices to WiFi:"
echo    "        Network:   ${AP_SSID}"
echo    "        Password:  (the one you just set)"
echo ""
if [[ -z "${TS_KEY:-}" ]]; then
    echo -e "  ${Y}Tailscale not configured yet.${NC}"
    echo    "        After reboot: sudo tailscale up"
    echo ""
fi
echo -e "  ${BLD}Useful commands${NC}"
echo -e "    sudo travel-status    — live status overview"
echo -e "    sudo travel-tui       — interactive management dashboard"
echo -e "    sudo update-router    — pull and apply latest update"
echo ""
echo -e "  ${C}Config:${NC}  /etc/default/travel-router"
echo -e "  ${C}Log:${NC}     ${LOG}"
echo -e "  ${C}Version:${NC} ${INSTALLED_VERSION}"
echo ""
