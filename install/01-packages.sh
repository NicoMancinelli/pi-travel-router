#!/bin/bash
# install/01-packages.sh — package installation
# Defines run_packages(). Source this file; do not execute directly.

run_packages() {
    section "Installing packages"

    run_or_dry apt-get update -qq

    # Core packages
    run_or_dry env DEBIAN_FRONTEND=noninteractive apt-get install -y \
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

    ok "Core packages installed"

    # log2ram (external repo)
    if ! dpkg -l log2ram &>/dev/null; then
        info "Installing log2ram"
        echo "deb [signed-by=/usr/share/keyrings/azlux-archive-keyring.gpg] http://packages.azlux.fr/debian/ bookworm main" \
            > /etc/apt/sources.list.d/azlux.list
        curl -s https://azlux.fr/repo.gpg.key | gpg --dearmor -o /tmp/azlux.gpg
        _AZLUX_FP=$(gpg --no-default-keyring --keyring /tmp/azlux.gpg --fingerprint 2>/dev/null \
            | tr -d ' \n' | grep -oi '[0-9A-F]\{40\}' | head -1 || true)
        _AZLUX_EXPECTED="7ACDC3E7BB726C780FFA4C5C6C26D5E78B89A06B"
        if [[ "${_AZLUX_FP^^}" != "$_AZLUX_EXPECTED" ]]; then
            rm -f /tmp/azlux.gpg
            die "log2ram GPG key fingerprint mismatch — aborting (got: ${_AZLUX_FP:-empty})"
        fi
        mv /tmp/azlux.gpg /usr/share/keyrings/azlux-archive-keyring.gpg
        chmod 644 /usr/share/keyrings/azlux-archive-keyring.gpg
        run_or_dry apt-get update -qq
        run_or_dry env DEBIAN_FRONTEND=noninteractive apt-get install -y log2ram
    fi
    ok "log2ram installed"

    # Tailscale
    if ! command -v tailscale &>/dev/null; then
        info "Installing Tailscale"
        run_or_dry curl -fsSL https://tailscale.com/install.sh | sh
    fi
    ok "Tailscale installed"

    # RaspAP
    if ! dpkg -l raspap-webgui &>/dev/null && [[ ! -d /etc/raspap ]]; then
        info "Installing RaspAP"
        run_or_dry curl -sL https://install.raspap.com | bash -s -- --yes --wireguard 0 --ad-blocker 0 --openvpn 0
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
            python3 -c "
import sys, re
path, pwd = sys.argv[1], sys.argv[2]
with open(path) as f: content = f.read()
content = re.sub(r'(password|pass)\s*=\s*[\"']?[A-Za-z0-9!@#\$%^&*_-]*[\"']?', r'\1 = \"' + pwd + '\"', content, flags=re.IGNORECASE)
with open(path, 'w') as f: f.write(content)
" "$_RASPAP_AUTH" "$RASPAP_PASS" 2>/dev/null || true
            ok "RaspAP password rotated (stored in $_RASPAP_AUTH)"
        else
            _RASPAP_AUTH_DIR="/etc/raspap"
            mkdir -p "$_RASPAP_AUTH_DIR"
            printf 'admin:%s\n' "$RASPAP_PASS" > "$_RASPAP_AUTH_DIR/raspap.auth"
            chmod 640 "$_RASPAP_AUTH_DIR/raspap.auth"
            ok "RaspAP auth file created at $_RASPAP_AUTH_DIR/raspap.auth"
        fi
        # Export for finalize summary
        RASPAP_PASS_DISPLAY="$RASPAP_PASS"
        export RASPAP_PASS_DISPLAY
    fi

    # Extra monitoring tools
    run_or_dry env DEBIAN_FRONTEND=noninteractive apt-get install -y bmon iftop 2>/dev/null || true
    ok "Real-time traffic tools installed (bmon, iftop)"
}
