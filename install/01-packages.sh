#!/bin/bash
# install/01-packages.sh — package installation
# Defines run_packages(). Source this file; do not execute directly.

run_packages() {
    section "Installing packages"

    run_or_dry apt-get update -qq

    # libimobiledevice was renamed in Debian 13 (trixie); detect which name is available
    _libimob="libimobiledevice6"
    apt-cache show libimobiledevice6 &>/dev/null 2>&1 || _libimob="libimobiledevice-1.0-6"

    # Core packages
    run_or_dry env DEBIAN_FRONTEND=noninteractive apt-get install -y \
        hostapd dnsmasq iptables iptables-persistent netfilter-persistent \
        curl wget git jq \
        usbmuxd "${_libimob}" libimobiledevice-utils ipheth-utils \
        macchanger vnstat \
        stubby \
        unattended-upgrades \
        bluez bluez-tools python3-dbus \
        avahi-daemon \
        iproute2 iw wireless-tools \
        qrencode \
        radvd

    ok "Core packages installed"

    # log2ram (external repo)
    # log2ram only publishes up to bookworm; use that repo for any newer release too
    _log2ram_suite="$(grep '^VERSION_CODENAME=' /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"' || echo bookworm)"
    case "${_log2ram_suite}" in bullseye|bookworm) ;; *) _log2ram_suite="bookworm" ;; esac

    if ! dpkg -l log2ram &>/dev/null; then
        info "Installing log2ram"
        echo "deb [signed-by=/usr/share/keyrings/azlux-archive-keyring.gpg] http://packages.azlux.fr/debian/ ${_log2ram_suite} main" \
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
    # Extra monitoring tools
    run_or_dry env DEBIAN_FRONTEND=noninteractive apt-get install -y bmon iftop 2>/dev/null || true
    ok "Real-time traffic tools installed (bmon, iftop)"
}
