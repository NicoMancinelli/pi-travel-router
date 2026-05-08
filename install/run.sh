#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  install/run.sh — Modular installer entry point (v2)                        ║
# ║                                                                              ║
# ║  This is the NEW modular orchestrator for pi-travel-router.                 ║
# ║  It sources all install/[0-9][0-9]-*.sh modules and runs them in order.     ║
# ║                                                                              ║
# ║  The original install.sh at the repo root remains UNCHANGED and works as   ║
# ║  a fallback / reference implementation. Do not remove it until this         ║
# ║  modular path has been validated on target hardware.                         ║
# ║                                                                              ║
# ║  Usage: sudo bash install/run.sh [options]                                  ║
# ║                                                                              ║
# ║  Options:                                                                    ║
# ║    --check, --dry-run   Print what would happen without making changes       ║
# ║    --module=NAME        Run only the named module (e.g. --module=03-vpn)    ║
# ║    --skip=NAME          Skip a specific module (e.g. --skip=07-monitoring)  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

set -euo pipefail

# Resolve repo root regardless of where the script is called from
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO

# ── Logging setup (mirrors install.sh) ──────────────────────────────────────────
LOG="/var/log/firstboot-install.log"
install -m 600 -o root -g root /dev/null "$LOG" 2>/dev/null || true
exec > >(tee -a "$LOG") 2>&1

# ── Source shared helpers ────────────────────────────────────────────────────────
# shellcheck source=install/lib/common.sh
source "${REPO}/install/lib/common.sh"

echo ""
echo "  Pi Zero 2 W Travel Router — Modular Installer (v2)"
echo "  Log: $LOG"
echo ""

# ── Parse CLI arguments ──────────────────────────────────────────────────────────
DRY_RUN=0
ONLY_MODULE=""
SKIP_MODULE=""

for arg in "$@"; do
    case "${arg}" in
        --check|--dry-run)
            DRY_RUN=1
            warn "DRY-RUN mode — no changes will be made"
            ;;
        --module=*)
            ONLY_MODULE="${arg#--module=}"
            info "Running only module: ${ONLY_MODULE}"
            ;;
        --skip=*)
            SKIP_MODULE="${arg#--skip=}"
            info "Skipping module: ${SKIP_MODULE}"
            ;;
        --help|-h)
            grep '^# ║' "${BASH_SOURCE[0]}" | sed 's/^# ║  \?/  /'
            exit 0
            ;;
        *)
            warn "Unknown argument: ${arg} (ignored)"
            ;;
    esac
done

export DRY_RUN

# ── Config prompts (identical to install.sh) ─────────────────────────────────────
section "Configuration"

_yn() {
    local v="${!1:-}"
    [[ "$v" =~ ^[01]$ ]] && return
    if [[ "${INSTALL_NONINTERACTIVE:-0}" == "1" ]]; then
        printf -v "$1" '%s' "0"
        return
    fi
    read -rp "  $2 [y/N] " _r
    printf -v "$1" '%s' "$([[ "$_r" =~ ^[Yy]$ ]] && echo 1 || echo 0)"
}

validate_flag() {
    local name=$1 value=${!1:-0}
    [[ "$value" =~ ^[01]$ ]] || die "$name must be 0 or 1"
}

if [[ "${INSTALL_NONINTERACTIVE:-0}" != "1" ]]; then
    read -rp "  AP SSID [TravelRouter]: " AP_SSID;          AP_SSID="${AP_SSID:-TravelRouter}"
    if printf '%s' "$AP_SSID" | LC_ALL=C grep -qP '[\x00-\x1f]'; then
        die "AP SSID may not contain control characters"
    fi
    read -rsp "  AP passphrase (8+ chars): " AP_PASS;        echo
    if printf '%s' "$AP_PASS" | LC_ALL=C grep -qP '[\x00-\x1f\x7f-\xff]'; then
        die "AP passphrase must use printable ASCII only"
    fi
    read -rp "  WiFi country code [US]: " COUNTRY;           COUNTRY="${COUNTRY:-US}"
    read -rp "  ntfy.sh topic (blank = no notifications): " NTFY_TOPIC; NTFY_TOPIC="${NTFY_TOPIC:-}"
    read -rsp "  Tailscale auth key (tskey-auth-... or blank): " TS_KEY; echo; TS_KEY="${TS_KEY:-}"
    read -rp  "  SSH admin public key (paste pubkey, or blank): " SSH_ADMIN_KEY; SSH_ADMIN_KEY="${SSH_ADMIN_KEY:-}"
    read -rp  "  Headscale URL (blank = use Tailscale cloud): " HEADSCALE_URL; HEADSCALE_URL="${HEADSCALE_URL:-}"

    _yn ENABLE_BLOCKLISTS          "Enable threat-intel blocklist (Firehol L1)?"
    _yn ENABLE_TOR_TRANSPARENT     "Enable Tor transparent proxy?"
    _yn ENABLE_HTTP_UA_REWRITE     "Enable HTTP User-Agent normalization (privoxy)?"
    _yn ENABLE_OPEN_WIFI_FALLBACK  "Enable open WiFi fallback (join any open network)?"
    _yn ENABLE_DOT                 "Enable DNS-over-TLS (stubby → Cloudflare + Quad9)?"
    _yn ENABLE_VPN_KILLSWITCH      "Enable VPN kill switch (block AP traffic if Tailscale drops)?"
    _yn ENABLE_AUTO_UPDATES        "Enable automatic OS security updates (unattended-upgrades)?"
    _yn ENABLE_ADGUARD             "Enable AdGuard Home (DNS ad-blocker + per-client analytics)?"
    _yn ENABLE_AVAHI_REFLECTOR     "Enable mDNS reflector (AirPrint/AirPlay over Tailscale)?"
    _yn ENABLE_AP_SCHEDULE         "Enable scheduled AP disable at night (02:00–07:00)?"
    _yn ENABLE_CLIENT_QOS          "Enable per-client bandwidth fairness (CAKE per-host on uap0)?"
    _yn ENABLE_PER_DEVICE_VPN      "Enable per-device Tailscale routing (specific MACs via VPN)?"
    _yn ENABLE_CAKE_AUTOTUNE       "Enable automatic CAKE bandwidth tuning (weekly speedtest on wlan0)?"
    _yn ENABLE_SPLIT_TUNNEL        "Enable domain-based split tunnel (route specific domains via Tailscale)?"
    if [[ "${ENABLE_SPLIT_TUNNEL:-0}" = "1" && -z "${SPLIT_TUNNEL_DOMAINS:-}" ]]; then
        read -rp "  Split tunnel domains (space-separated): " SPLIT_TUNNEL_DOMAINS
        SPLIT_TUNNEL_DOMAINS="${SPLIT_TUNNEL_DOMAINS:-}"
    fi
    _yn ENABLE_2FA                 "Enable SSH two-factor authentication (TOTP)?"
    _yn ENABLE_BANDWIDTH_DASHBOARD "Enable bandwidth analytics dashboard (daily HTML report)?"
    _yn ENABLE_PROMETHEUS_EXPORTER "Enable Prometheus node exporter on :9100?"
    _yn ENABLE_UPS_MONITOR         "Enable PiSugar UPS battery monitor (safe shutdown at low battery)?"

    if [[ "${ENABLE_TOR_TRANSPARENT:-0}" = "1" && -z "${TOR_AP_PASS:-}" ]]; then
        read -rsp "  Tor AP passphrase (8+ chars, for TorAP SSID): " TOR_AP_PASS; echo
        [[ ${#TOR_AP_PASS} -ge 8 ]] || die "Tor AP passphrase must be 8+ characters"
    fi
fi

# Defaults
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
        [[ ${#TOR_AP_PASS} -ge 8 ]] || die "Set TOR_AP_PASS (8+ chars) when ENABLE_TOR_TRANSPARENT=1"
    fi
fi

# Validation
[[ -n "$AP_SSID" && ${#AP_SSID} -le 32 ]] || die "SSID must be 1-32 characters"
[[ ${#AP_PASS} -ge 8 && ${#AP_PASS} -le 63 ]] || die "Passphrase must be 8-63 characters"
[[ "$AP_PASS" =~ '#' ]] && die "AP passphrase must not contain '#' (hostapd comment character)"
[[ "${TOR_AP_PASS:-}" =~ '#' ]] && die "Tor AP passphrase must not contain '#'"
[[ "$COUNTRY" =~ ^[A-Za-z]{2}$ ]] || die "Country code must be two letters, e.g. US"
COUNTRY="${COUNTRY^^}"
[[ "$NTFY_TOPIC" =~ ^[A-Za-z0-9._-]*$ ]] || die "ntfy.sh topic may only contain letters, numbers, dot, underscore, or dash"
if [[ -n "$TS_KEY" && -z "$HEADSCALE_URL" ]]; then
    [[ "$TS_KEY" =~ ^tskey-auth- ]] || die "Tailscale auth key must start with tskey-auth-"
fi

# Validate optional hostname
if [[ -n "${ROUTER_HOSTNAME:-}" ]]; then
    [[ "$ROUTER_HOSTNAME" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]] || \
        die "ROUTER_HOSTNAME '${ROUTER_HOSTNAME}' is invalid"
fi

# Validate AP schedule times
if [[ -n "${AP_DISABLE_TIME:-}" ]] && ! [[ "$AP_DISABLE_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    die "AP_DISABLE_TIME must be in HH:MM format (00:00–23:59)"
fi
if [[ -n "${AP_ENABLE_TIME:-}" ]] && ! [[ "$AP_ENABLE_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    die "AP_ENABLE_TIME must be in HH:MM format (00:00–23:59)"
fi

ENABLE_WAN_METRICS="${ENABLE_WAN_METRICS:-1}"
for flag in ENABLE_OPEN_WIFI_FALLBACK ENABLE_HTTP_UA_REWRITE ENABLE_TOR_TRANSPARENT \
    ENABLE_BLOCKLISTS ENABLE_DOT ENABLE_VPN_KILLSWITCH ENABLE_AUTO_UPDATES \
    ENABLE_AVAHI_REFLECTOR ENABLE_ADGUARD ENABLE_AP_SCHEDULE ENABLE_CLIENT_QOS \
    ENABLE_PER_DEVICE_VPN ENABLE_CAKE_AUTOTUNE ENABLE_SPLIT_TUNNEL ENABLE_2FA \
    ENABLE_WAN_METRICS ENABLE_BANDWIDTH_DASHBOARD ENABLE_PROMETHEUS_EXPORTER \
    ENABLE_UPS_MONITOR ENABLE_WIREGUARD; do
    validate_flag "$flag"
done

echo ""
info "SSID:      $AP_SSID"
info "Country:   $COUNTRY"
info "ntfy:      ${NTFY_TOPIC:-disabled}"
info "Tailscale: ${TS_KEY:+key provided}${TS_KEY:-will auth manually after install}"
echo ""

if [[ "${INSTALL_NONINTERACTIVE:-0}" != "1" ]]; then
    read -rp "  Proceed? [y/N] " CONFIRM
    [[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

# Apply hostname
if [[ -n "${ROUTER_HOSTNAME:-}" && "$ROUTER_HOSTNAME" != "travelrouter" ]]; then
    hostnamectl set-hostname "$ROUTER_HOSTNAME" 2>/dev/null || true
    python3 -c "
import sys, tempfile, os
with open('/etc/hosts') as f: content = f.read()
new = content.replace('travelrouter', sys.argv[1])
fd, tmp = tempfile.mkstemp(dir='/etc')
try:
    with os.fdopen(fd, 'w') as f: f.write(new)
    os.replace(tmp, '/etc/hosts')
except:
    os.unlink(tmp); raise
" "$ROUTER_HOSTNAME" 2>/dev/null || true
    echo "$ROUTER_HOSTNAME" > /etc/hostname
    ok "Hostname set to $ROUTER_HOSTNAME"
fi

# Apply timezone
if [[ -n "${ROUTER_TIMEZONE:-}" ]]; then
    [[ "$ROUTER_TIMEZONE" =~ ^[A-Za-z][A-Za-z0-9/_+-]{1,49}$ ]] || die "Invalid ROUTER_TIMEZONE: $ROUTER_TIMEZONE"
    timedatectl set-timezone "$ROUTER_TIMEZONE" 2>/dev/null || true
    ok "Timezone set to $ROUTER_TIMEZONE"
fi

# ── Source all modules ────────────────────────────────────────────────────────────
for _module_file in "${REPO}"/install/[0-9][0-9]-*.sh; do
    # shellcheck source=/dev/null
    source "${_module_file}"
done

# ── Helper: should we run a given module? ─────────────────────────────────────────
_should_run() {
    local _module_name="$1"
    # If --module=X was given, only run that module
    if [[ -n "$ONLY_MODULE" && "$_module_name" != *"$ONLY_MODULE"* ]]; then
        return 1
    fi
    # If --skip=X was given, skip that module
    if [[ -n "$SKIP_MODULE" && "$_module_name" == *"$SKIP_MODULE"* ]]; then
        info "Skipping module: $_module_name"
        return 1
    fi
    return 0
}

# ── Run all phases ────────────────────────────────────────────────────────────────
_should_run "00-validate"  && run_validate
_should_run "01-packages"  && run_packages
_should_run "02-networking" && run_networking
_should_run "03-vpn"        && run_vpn
_should_run "04-dns"        && run_dns
_should_run "05-firewall"   && run_firewall
_should_run "06-failover"   && run_failover
_should_run "07-monitoring" && run_monitoring
_should_run "08-security"   && run_security
_should_run "09-services"   && run_services
_should_run "10-finalize"   && run_finalize

echo "Install complete."
