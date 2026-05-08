#!/bin/bash
# install/02-networking.sh — networking stack (AP, dnsmasq, NM, sysctl, boot config)
# Defines run_networking(). Source this file; do not execute directly.

run_networking() {
    section "Boot config — USB gadget mode (dwc2/g_ncm)"

    local CONFIG_TXT="/boot/firmware/config.txt"
    [[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="/boot/config.txt"

    if ! grep -q "dtoverlay=dwc2" "$CONFIG_TXT"; then
        { echo ""; echo "[all]"; echo "dtoverlay=dwc2,dr_mode=peripheral"; echo "dtoverlay=watchdog"; } >> "$CONFIG_TXT"
        ok "dwc2 overlay added to $CONFIG_TXT"
    else
        ok "dwc2 overlay already present"
    fi

    echo "dwc2"       > /etc/modules-load.d/dwc2.conf
    echo "g_ncm"      > /etc/modules-load.d/g-ncm.conf
    echo "tcp_bbr"    > /etc/modules-load.d/tcp_bbr.conf
    echo "bcm2835_wdt" > /etc/modules-load.d/watchdog.conf
    ok "Module load configs written"

    # ── Sysctl ──────────────────────────────────────────────────────────────────
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

    cat > /etc/sysctl.d/99-bbr.conf << 'EOF'
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF

    # uap0: keep IPv6 enabled for radvd RA (idempotent)
    if ! grep -q "uap0" /etc/sysctl.d/99-disable-ipv6-uplink.conf 2>/dev/null; then
        cat >> /etc/sysctl.d/99-disable-ipv6-uplink.conf << 'EOF'

# uap0: keep IPv6 enabled so radvd can send Router Advertisements to AP clients
net.ipv6.conf.uap0.disable_ipv6 = 0
EOF
    fi

    sysctl -p /etc/sysctl.d/99-tailscale.conf &>/dev/null || true
    sysctl -p /etc/sysctl.d/99-disable-ipv6-uplink.conf &>/dev/null || true
    ok "Sysctl configured"

    # ── NetworkManager ──────────────────────────────────────────────────────────
    section "NetworkManager — usb0 static IP (USB gadget)"

    local USB0_CONN="/etc/NetworkManager/system-connections/usb0-firstboot.nmconnection"
    if [[ ! -f "$USB0_CONN" ]]; then
        install_file config/usb0-firstboot.nmconnection "$USB0_CONN" 600
        nmcli connection reload 2>/dev/null || true
        ok "usb0 NM profile installed"
    else
        ok "usb0 NM profile already present"
    fi

    section "NetworkManager config"
    mkdir -p /etc/NetworkManager/conf.d
    install_file config/NetworkManager-wifi-random-mac.conf /etc/NetworkManager/conf.d/wifi-random-mac.conf

    cat > /etc/NetworkManager/conf.d/wifi-powersave.conf << 'EOF'
# Disable WiFi power save on uplink STA interface
[connection]
wifi.powersave = 2
EOF
    ok "NetworkManager: MAC randomization + power save off"

    # ── brcmfmac driver tuning ──────────────────────────────────────────────────
    section "brcmfmac — disable firmware roaming engine"
    cat > /etc/modprobe.d/brcmfmac.conf << 'EOF'
options brcmfmac roamoff=1 feature_disable=0x82000
EOF
    ok "brcmfmac roamoff=1 feature_disable=0x82000"

    # ── wpa_supplicant — optional open WiFi fallback ────────────────────────────
    section "wpa_supplicant — optional open WiFi fallback"
    if [[ "${ENABLE_OPEN_WIFI_FALLBACK:-0}" = "1" && ! -f /etc/wpa_supplicant/wpa_supplicant.conf ]]; then
        cat > /etc/wpa_supplicant/wpa_supplicant.conf << EOF
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=${COUNTRY:-US}

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
    else
        ok "Open WiFi fallback disabled by default"
    fi

    # ── hostapd ─────────────────────────────────────────────────────────────────
    section "hostapd — 802.11n + DTIM tuning"

    if [[ -f /etc/hostapd/hostapd.conf ]]; then
        local _existing_ssid
        _existing_ssid=$(python3 -c "
with open('/etc/hostapd/hostapd.conf') as f:
    for l in f:
        if l.startswith('ssid='):
            print(l.strip()[5:])
            break
" 2>/dev/null || true)
        if [[ -n "$_existing_ssid" ]]; then
            warn "hostapd.conf already exists — preserving existing SSID/passphrase"
            AP_SSID="${AP_SSID:-$_existing_ssid}"
        fi
        ok "hostapd.conf already present — not overwritten (SSID=${AP_SSID:-unknown})"
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
hw_mode=g
wpa_passphrase=PLACEHOLDER_PASS
interface=uap0
wpa=2
wpa_pairwise=CCMP
country_code=${COUNTRY:-US}

ieee80211n=1
wmm_enabled=1
ht_capab=[HT40][SHORT-GI-20][DSSS_CCK-40]
dtim_period=1
EOF
        # Write SSID and passphrase safely via Python (C6)
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
" "${AP_SSID:-TravelRouter}" "${AP_PASS:?AP_PASS required}"
        ok "hostapd configured: SSID=${AP_SSID:-TravelRouter}"
    fi

    # Apply regulatory domain
    if command -v iw &>/dev/null; then
        iw reg set "${COUNTRY:-US}" 2>/dev/null && ok "iw: regulatory domain set to ${COUNTRY:-US}" \
            || warn "iw reg set ${COUNTRY:-US} failed (non-fatal)"
    fi

    # ── dnsmasq ─────────────────────────────────────────────────────────────────
    section "dnsmasq config"

    install_file config/dnsmasq-travel-tweaks.conf /etc/dnsmasq.d/travel-tweaks.conf

    if ! grep -q "stop-dns-rebind" /etc/dnsmasq.d/travel-tweaks.conf; then
        cat >> /etc/dnsmasq.d/travel-tweaks.conf << 'EOF'

# DNS rebinding protection
stop-dns-rebind
rebind-localhost-ok
rebind-domain-ok=/local/
rebind-domain-ok=/lan/
EOF
    fi

    install_file config/dnsmasq-usb-gadget.conf   /etc/dnsmasq.d/usb-gadget.conf
    install_file config/dnsmasq-static-leases.conf /etc/dnsmasq.d/static-leases.conf
    ok "dnsmasq configs installed"

    # ── NM WAN metrics dispatcher ───────────────────────────────────────────────
    section "WAN metric auto-management"
    install_file config/nm-wan-metrics /etc/NetworkManager/dispatcher.d/50-wan-metrics 755
    if [[ "${ENABLE_WAN_METRICS:-1}" = "1" ]]; then
        ok "WAN metric dispatcher installed"
    else
        rm -f /etc/NetworkManager/dispatcher.d/50-wan-metrics
        ok "WAN metric dispatcher disabled"
    fi

    # ── IPv6 — DHCPv6 uplink + radvd ───────────────────────────────────────────
    section "IPv6 — DHCPv6 uplink + SLAAC on AP (radvd)"
    install_file config/dhclient6.conf /etc/dhclient6.conf 644
    mkdir -p /etc/NetworkManager/dispatcher.d
    install_file config/nm-dispatcher/70-dhcpv6-uplink.sh \
        /etc/NetworkManager/dispatcher.d/70-dhcpv6-uplink 755
    ok "DHCPv6 uplink dispatcher installed"

    if command -v radvd >/dev/null 2>&1; then
        install_file config/radvd.conf /etc/radvd.conf 644
        systemctl enable radvd 2>/dev/null || true
        ok "radvd: RA config installed for uap0"
    else
        warn "radvd not found — SLAAC/RA on uap0 will not work"
    fi

    # ── rc.local ─────────────────────────────────────────────────────────────────
    section "rc.local — AP interface + channel sync + power save"
    install_file config/rc.local /etc/rc.local 755
    ok "rc.local installed"

    # ── udev rules ───────────────────────────────────────────────────────────────
    section "udev rules"
    install_file config/90-ipheth.rules             /etc/udev/rules.d/90-ipheth.rules 644
    install_file config/99-apple-autosuspend.rules  /etc/udev/rules.d/99-apple-autosuspend.rules 644
    install_file config/91-android-tether.rules     /etc/udev/rules.d/91-android-tether.rules 644
    install_file config/modules-android-tether.conf /etc/modules-load.d/android-tether.conf 644
    udevadm control --reload-rules 2>/dev/null || true
    ok "udev rules installed (ipheth + Android tethering + USB autosuspend)"

    # ── log2ram ──────────────────────────────────────────────────────────────────
    section "log2ram"
    if [[ -f /etc/log2ram.conf ]]; then
        sed -i 's/^SIZE=.*/SIZE=128M/' /etc/log2ram.conf
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

    # ── Avahi ────────────────────────────────────────────────────────────────────
    section "Avahi — mDNS reflector"
    install_file config/avahi-daemon.conf /etc/avahi/avahi-daemon.conf 644
    if [[ "${ENABLE_AVAHI_REFLECTOR:-0}" = "1" ]]; then
        systemctl enable --now avahi-daemon 2>/dev/null || true
        ok "Avahi mDNS reflector enabled"
    else
        systemctl disable --now avahi-daemon 2>/dev/null || true
        ok "Avahi installed but disabled (set ENABLE_AVAHI_REFLECTOR=1 to activate)"
    fi

    # ── nftables TTL/DSCP ────────────────────────────────────────────────────────
    section "nftables TTL/DSCP rules"
    mkdir -p /etc/nftables.conf.d
    install_file config/nftables-travel-router.nft /etc/nftables.conf.d/travel-router.nft 644
    if ! grep -q "nftables.conf.d" /etc/nftables.conf 2>/dev/null; then
        printf '\ninclude "/etc/nftables.conf.d/*.nft"\n' >> /etc/nftables.conf
    fi
    nft -f /etc/nftables.conf.d/travel-router.nft 2>/dev/null || \
        warn "nft load failed — rules will apply on next nftables service start"
    systemctl enable nftables 2>/dev/null || true
    ok "nftables TTL/DSCP rules loaded"

    # ── Hardware watchdog ─────────────────────────────────────────────────────────
    section "Hardware watchdog (BCM2835)"
    if ! grep -q "RuntimeWatchdogSec" /etc/systemd/system.conf 2>/dev/null; then
        cat >> /etc/systemd/system.conf << 'EOF'

# Hardware watchdog — reboots Pi if kernel locks up
RuntimeWatchdogSec=15
ShutdownWatchdogSec=5min
EOF
    fi
    ok "Hardware watchdog enabled (RuntimeWatchdogSec=15 — needs reboot to activate)"

    # ── Log rotation ──────────────────────────────────────────────────────────────
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

    # ── usbmuxd hardening ─────────────────────────────────────────────────────────
    section "usbmuxd hardening"
    mkdir -p /etc/systemd/system/usbmuxd.service.d
    cat > /etc/systemd/system/usbmuxd.service.d/restart.conf << 'EOF'
[Service]
Restart=on-failure
RestartSec=3
CPUQuota=20%
EOF
    systemctl daemon-reload
    systemctl enable usbmuxd  2>/dev/null || true
    systemctl enable bluetooth 2>/dev/null || true
    ok "usbmuxd: Restart=on-failure, CPUQuota=20%"
}
