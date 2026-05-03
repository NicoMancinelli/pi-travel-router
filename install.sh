#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║   pi-travel-router — Installer                                          ║
# ║   Raspberry Pi Zero 2 W + Pi OS Lite Bookworm                          ║
# ║   https://github.com/NicoMancinelli/pi-travel-router                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝
#
# Usage: sudo bash install.sh
# Run from the cloned repo root on a fresh Pi OS Lite Bookworm install.
# A reboot is required at the end to activate dwc2/g_ether USB gadget mode.

set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="/tmp/travel-router-install.log"
exec > >(tee -a "$LOG") 2>&1

# ── Colours ───────────────────────────────────────────────────────────────────
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; C='\033[0;36m'; NC='\033[0m'
ok()      { echo -e "${G}✓${NC} $*"; }
info()    { echo -e "${C}→${NC} $*"; }
warn()    { echo -e "${Y}⚠${NC} $*"; }
die()     { echo -e "${R}✗ FATAL:${NC} $*" >&2; exit 1; }
section() { echo -e "\n${C}━━ $* ━━${NC}"; }
validate_flag() {
    local name=$1 value=${!1:-0}
    [[ "$value" =~ ^[01]$ ]] || die "$name must be 0 or 1"
}

# ── Guards ────────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && die "Run as root: sudo bash install.sh"
[[ -f "$REPO/scripts/wan-watchdog.sh" ]] || die "Run from repo root (scripts/ not found)"
uname -m | grep -qE 'armv7l|aarch64' || warn "Expected armv7l/aarch64 — got $(uname -m)"
grep -q bookworm /etc/os-release 2>/dev/null || warn "Expected Bookworm — continuing anyway"

echo ""
echo "  Pi Zero 2 W Travel Router — Installer"
echo "  Log: $LOG"
echo ""

# ── Config prompts ────────────────────────────────────────────────────────────
section "Configuration"

read -rp "  AP SSID [TravelRouter]: " AP_SSID;          AP_SSID="${AP_SSID:-TravelRouter}"
read -rsp "  AP passphrase (8+ chars): " AP_PASS;        echo
read -rp "  WiFi country code [US]: " COUNTRY;           COUNTRY="${COUNTRY:-US}"
read -rp "  ntfy.sh topic (blank = no notifications): " NTFY_TOPIC; NTFY_TOPIC="${NTFY_TOPIC:-}"
read -rsp "  Tailscale auth key (tskey-auth-... or blank): " TS_KEY; echo; TS_KEY="${TS_KEY:-}"

# Optional features — env-var pre-set wins (allows scripted/non-interactive installs)
_yn() { local v="${!1:-}"; [[ "$v" =~ ^[01]$ ]] && return; read -rp "  $2 [y/N] " _r; printf -v "$1" '%s' "$([[ "$_r" =~ ^[Yy]$ ]] && echo 1 || echo 0)"; }
_yn ENABLE_BLOCKLISTS          "Enable threat-intel blocklist (Firehol L1)?"
_yn ENABLE_TOR_TRANSPARENT     "Enable Tor transparent proxy?"
_yn ENABLE_HTTP_UA_REWRITE     "Enable HTTP User-Agent normalization (privoxy)?"
_yn ENABLE_OPEN_WIFI_FALLBACK  "Enable open WiFi fallback (join any open network)?"
_yn ENABLE_DOT                 "Enable DNS-over-TLS (stubby → Cloudflare + Quad9)?"
_yn ENABLE_VPN_KILLSWITCH      "Enable VPN kill switch (block AP traffic if Tailscale drops)?"
_yn ENABLE_AUTO_UPDATES        "Enable automatic OS security updates (unattended-upgrades)?"

if [[ "${ENABLE_TOR_TRANSPARENT:-0}" = "1" && -z "${TOR_AP_PASS:-}" ]]; then
    read -rsp "  Tor AP passphrase (8+ chars, for TorAP SSID): " TOR_AP_PASS; echo
    [[ ${#TOR_AP_PASS} -ge 8 ]] || die "Tor AP passphrase must be 8+ characters"
fi

[[ -n "$AP_SSID" && ${#AP_SSID} -le 32 ]] || die "SSID must be 1-32 characters"
[[ ${#AP_PASS} -ge 8 && ${#AP_PASS} -le 63 ]] || die "Passphrase must be 8-63 characters"
[[ "$COUNTRY" =~ ^[A-Za-z]{2}$ ]] || die "Country code must be two letters, e.g. US"
COUNTRY="${COUNTRY^^}"
[[ "$NTFY_TOPIC" =~ ^[A-Za-z0-9._-]*$ ]] || die "ntfy.sh topic may only contain letters, numbers, dot, underscore, or dash"
[[ -z "$TS_KEY" || "$TS_KEY" =~ ^tskey-auth- ]] || die "Tailscale auth key must start with tskey-auth-"
for flag in ENABLE_OPEN_WIFI_FALLBACK ENABLE_HTTP_UA_REWRITE ENABLE_TOR_TRANSPARENT ENABLE_BLOCKLISTS ENABLE_DOT ENABLE_VPN_KILLSWITCH ENABLE_AUTO_UPDATES; do
    validate_flag "$flag"
done

echo ""
info "SSID:      $AP_SSID"
info "Country:   $COUNTRY"
info "ntfy:      ${NTFY_TOPIC:-disabled}"
info "Tailscale: ${TS_KEY:+key provided}${TS_KEY:-will auth manually after install}"
echo ""
read -rp "  Proceed? [y/N] " CONFIRM
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

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
    dhcpcd5 curl wget git jq \
    usbmuxd libimobiledevice6 libimobiledevice-utils ipheth-utils \
    macchanger vnstat \
    privoxy \
    tor \
    stubby \
    unattended-upgrades \
    bluez bluez-tools python3-dbus \
    iproute2 iw wireless-tools

ok "Packages installed"

# log2ram
if ! dpkg -l log2ram &>/dev/null; then
    info "Installing log2ram"
    echo "deb [signed-by=/usr/share/keyrings/azlux-archive-keyring.gpg] http://packages.azlux.fr/debian/ bookworm main" \
        > /etc/apt/sources.list.d/azlux.list
    curl -s https://azlux.fr/repo.gpg.key | gpg --dearmor -o /usr/share/keyrings/azlux-archive-keyring.gpg
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

# ── 2. Boot config (USB gadget mode) ─────────────────────────────────────────
section "Boot config — USB gadget mode (dwc2/g_ether)"

CONFIG_TXT="/boot/firmware/config.txt"
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="/boot/config.txt"

if ! grep -q "dtoverlay=dwc2" "$CONFIG_TXT"; then
    { echo ""; echo "[all]"; echo "dtoverlay=dwc2,dr_mode=peripheral"; } >> "$CONFIG_TXT"
    ok "dwc2 overlay added to $CONFIG_TXT"
else
    ok "dwc2 overlay already present"
fi

echo "dwc2"    > /etc/modules-load.d/dwc2.conf
echo "g_ether" > /etc/modules-load.d/g-ether.conf
echo "tcp_bbr" > /etc/modules-load.d/tcp_bbr.conf
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

if ! grep -q "net.core.default_qdisc=fq" /etc/sysctl.conf 2>/dev/null; then
    cat >> /etc/sysctl.conf << 'EOF'

# TCP BBR congestion control
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
fi

sysctl -p /etc/sysctl.d/99-tailscale.conf &>/dev/null || true
sysctl -p /etc/sysctl.d/99-disable-ipv6-uplink.conf &>/dev/null || true
ok "Sysctl configured"

# ── 4. dhcpcd — USB gadget static IP ─────────────────────────────────────────
section "dhcpcd — usb0 static IP (USB gadget)"

if ! grep -q "interface usb0" /etc/dhcpcd.conf 2>/dev/null; then
    cat >> /etc/dhcpcd.conf << 'EOF'

# USB gadget interface (g_ether) — Pi as Ethernet device for laptop
interface usb0
static ip_address=192.168.7.1/24
nohook wpa_supplicant
EOF
fi
ok "dhcpcd usb0 config added"

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
    ok "wpa_supplicant open fallback configured"
elif [[ "${ENABLE_OPEN_WIFI_FALLBACK:-0}" = "1" ]]; then
    warn "wpa_supplicant.conf already exists — open fallback not modified"
    warn "Manually add: network={key_mgmt=NONE priority=1 id_str=\"open-fallback\"}"
else
    ok "Open WiFi fallback disabled by default"
fi

# ── 8. hostapd ────────────────────────────────────────────────────────────────
section "hostapd — 802.11n + DTIM tuning"

cat > /etc/hostapd/hostapd.conf << EOF
driver=nl80211
ctrl_interface=/var/run/hostapd
ctrl_interface_group=0
beacon_int=100
auth_algs=1
wpa_key_mgmt=WPA-PSK
ssid=$AP_SSID
channel=6
hw_mode=g
wpa_passphrase=$AP_PASS
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
ok "hostapd configured: SSID=$AP_SSID"

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
    update-router.sh; do
    install_file "scripts/$script" "/usr/local/bin/$script" 755
    ok "  $script"
done

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
sed -i "s/^NTFY_TOPIC=.*/NTFY_TOPIC=\"${NTFY_TOPIC}\"/" /etc/default/travel-router
for flag in ENABLE_OPEN_WIFI_FALLBACK ENABLE_HTTP_UA_REWRITE ENABLE_TOR_TRANSPARENT ENABLE_BLOCKLISTS ENABLE_DOT ENABLE_VPN_KILLSWITCH ENABLE_AUTO_UPDATES; do
    sed -i "s/^${flag}=.*/${flag}=\"${!flag:-0}\"/" /etc/default/travel-router
done
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
    update-router.service update-router.timer; do
    install_file "systemd/$unit" "$SYSTEMD_DEST/$unit" 644
    ok "  $unit"
done

systemctl daemon-reload

for unit in \
    failover-watchdog.timer wan-watchdog.timer \
    cpu-performance.service cake-qdisc.service \
    wlan-mac-random.service \
    vnstat-metrics.timer update-blocklists.timer \
    update-router.timer; do
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
udevadm control --reload-rules 2>/dev/null || true
ok "udev rules installed (ipheth + USB autosuspend)"

# ── 15. log2ram ───────────────────────────────────────────────────────────────
section "log2ram"

if [[ -f /etc/log2ram.conf ]]; then
    sed -i 's/^SIZE=.*/SIZE=128M/' /etc/log2ram.conf
    grep -q "JOURNALD_AWARE" /etc/log2ram.conf && \
        sed -i 's/^JOURNALD_AWARE=.*/JOURNALD_AWARE=true/' /etc/log2ram.conf || \
        echo "JOURNALD_AWARE=true" >> /etc/log2ram.conf
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

        # Second BSS in hostapd.conf (uses same radio, separate SSID)
        if ! grep -q "^bss=uap1" /etc/hostapd/hostapd.conf; then
            cat >> /etc/hostapd/hostapd.conf << EOF

# Tor transparent-proxy AP (all traffic routed through Tor)
bss=uap1
ssid=TorAP
wpa=2
wpa_passphrase=${TOR_AP_PASS:-changeme}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
EOF
        fi

        install_file config/dnsmasq-tor-ap.conf /etc/dnsmasq.d/tor-ap.conf

        # Create uap1 at boot alongside uap0 in rc.local
        if ! grep -q "uap1" /etc/rc.local; then
            sed -i '/interface add uap0/a iw dev wlan0 interface add uap1 type __ap || true' /etc/rc.local
        fi

        ok "uap1 supported — TorAP SSID configured on 172.16.100.0/24"
    else
        warn "uap1 not supported by brcmfmac — Tor AP uses static-IP fallback"
        warn "  Clients: set static IP 172.16.100.x/24, GW+DNS 172.16.100.1 on the main AP"
    fi
else
    systemctl disable --now tor 2>/dev/null || true
    ok "Tor disabled by default"
fi

# ── 18. firewall rules ───────────────────────────────────────────────────────
section "Firewall — TTL, DSCP, isolation, optional proxy rules"

/usr/local/bin/travel-router-firewall.sh --save
ok "Firewall rules applied and saved"

# ── 19. Tailscale ─────────────────────────────────────────────────────────────
section "Tailscale"

systemctl enable --now tailscaled 2>/dev/null || true

if [[ -n "$TS_KEY" ]]; then
    # shellcheck disable=SC2206
    TS_ARGS=($TAILSCALE_UP_ARGS)
    if tailscale up \
        --authkey="$TS_KEY" \
        "${TS_ARGS[@]}" \
        2>/dev/null; then
        ok "Tailscale authenticated and subnet advertised"
    else
        warn "Tailscale auth failed — run manually: sudo tailscale up $TAILSCALE_UP_ARGS"
    fi
else
    warn "No Tailscale key provided. After reboot, run:"
    warn "  sudo tailscale up $TAILSCALE_UP_ARGS"
fi

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
echo "  Summary of what was installed:"
echo "    • RaspAP web UI (http://10.3.141.1 after boot)"
echo "    • AP SSID: $AP_SSID  (on uap0, 10.3.141.0/24)"
echo "    • USB gadget: usb0 → 192.168.7.1  (active after reboot)"
echo "    • iPhone USB tether: udev auto-detect (enx*, metric 100)"
echo "    • Bluetooth tether: set IPHONE_BT_MAC in /etc/default/travel-router"
echo "    • Uplink failover watchdog: 30s timer"
echo "    • WAN watchdog + captive portal detection: 60s timer"
echo "    • TTL=65 + DSCP strip (Visible carrier bypass)"
echo "    • DNS-over-TLS: ${ENABLE_DOT:-0}  (stubby → Cloudflare/Quad9)"
echo "    • VPN kill switch: ${ENABLE_VPN_KILLSWITCH:-0}  (AP traffic blocked if Tailscale drops)"
echo "    • privoxy: HTTP User-Agent normalization (${ENABLE_HTTP_UA_REWRITE:-0})"
echo "    • Tor: transparent proxy (${ENABLE_TOR_TRANSPARENT:-0})"
echo "    • Threat intel blocklist: daily timer installed, loading enabled=${ENABLE_BLOCKLISTS:-0}"
echo "    • Auto security updates: ${ENABLE_AUTO_UPDATES:-0}  (unattended-upgrades, reboot 03:30)"
echo "    • Auto-update: weekly check (Sun 03:00) — run manually: sudo update-router.sh"
echo "    • Installed version: $INSTALLED_VERSION  (cat /etc/travel-router-version)"
echo "    • Tailscale: subnet router for 10.3.141.0/24"
echo "    • TCP BBR + CAKE qdisc (bufferbloat control)"
echo "    • log2ram: /var/log in RAM"
echo "    • MAC randomization: wlan0 at boot"
echo "    • ntfy.sh: ${NTFY_TOPIC:-not configured (set NTFY_TOPIC in /etc/default/travel-router)}"
echo ""
echo "  Next steps:"
[[ -z "$TS_KEY" ]] && echo "    1. sudo tailscale up $TAILSCALE_UP_ARGS"
echo "    ${TS_KEY:+1}${TS_KEY:-2}. sudo reboot  ← activates USB gadget mode (dwc2) + log2ram"
echo "    3. Connect Mac via USB-C → ssh neek@192.168.7.1"
echo "    4. Edit /etc/default/travel-router to set NTFY_TOPIC + IPHONE_BT_MAC"
echo "    5. RaspAP web UI: http://10.3.141.1  (admin / secret)"
echo ""
echo "  Log saved to: $LOG"
echo ""
