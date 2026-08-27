#!/usr/bin/env bats
# Unit tests for build/stage-travel-router/files/imager-compat.sh

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
IMAGER_COMPAT_SCRIPT="${SCRIPT_DIR}/build/stage-travel-router/files/imager-compat.sh"

setup() {
    setup_mock_bin

    export _TEST_ROOT
    _TEST_ROOT="$(mktemp -d)"

    export STATE_DIR="${_TEST_ROOT}/var/lib/travel-router"
    mkdir -p "${STATE_DIR}"

    export BOOT_DIR="${_TEST_ROOT}/boot/firmware"
    mkdir -p "${BOOT_DIR}"
}

teardown() {
    teardown_mock_bin
    rm -rf "${_TEST_ROOT}"
}

@test "imager-compat: exits 0 cleanly when no firstrun or drop-in files exist" {
    run bash "${IMAGER_COMPAT_SCRIPT}"
    [ "$status" -eq 0 ]
}

@test "imager-compat: extracts hostname, country, timezone, wifi, and ssh key from firstrun.sh" {
    cat << 'EOF' > "${BOOT_DIR}/firstrun.sh"
#!/bin/bash
raspi-config nonint do_hostname mytravelrouter
raspi-config nonint do_wifi_country GB
raspi-config nonint do_change_timezone Europe/London
nmcli dev wifi connect "HotelWifi" password "SecretPass123"
echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKey user@laptop" >> /root/.ssh/authorized_keys
EOF

    cat << 'EOF' > "${BOOT_DIR}/cmdline.txt"
console=serial0,115200 root=PARTUUID=xxx systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot
EOF

    run bash "${IMAGER_COMPAT_SCRIPT}"
    [ "$status" -eq 0 ]

    # Verify original firstrun backup exists
    [ -f "${STATE_DIR}/firstrun.sh.orig" ]

    # Verify imager-preseed.json generated
    [ -f "${STATE_DIR}/imager-preseed.json" ]
    grep -q '"ROUTER_HOSTNAME": "mytravelrouter"' "${STATE_DIR}/imager-preseed.json"
    grep -q '"COUNTRY": "GB"' "${STATE_DIR}/imager-preseed.json"
    grep -q '"ROUTER_TIMEZONE": "Europe/London"' "${STATE_DIR}/imager-preseed.json"
    grep -q '"AP_SSID": "HotelWifi"' "${STATE_DIR}/imager-preseed.json"
    grep -q '"AP_PASS": "SecretPass123"' "${STATE_DIR}/imager-preseed.json"
    grep -q '"SSH_ADMIN_KEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKey user@laptop"' "${STATE_DIR}/imager-preseed.json"

    # Verify cmdline.txt cleaned
    ! grep -q 'systemd\.run=' "${BOOT_DIR}/cmdline.txt"

    # Verify firstrun.sh replaced with neutral stub
    grep -q 'Neutralised by pi-travel-router' "${BOOT_DIR}/firstrun.sh"
}

@test "imager-compat: parses drop-in travel-router.env file" {
    cat << 'EOF' > "${BOOT_DIR}/travel-router.env"
# Custom drop-in configuration
AP_SSID="DropInSSID"
AP_PASS="DropInPass123"
COUNTRY=DE
ROUTER_TIMEZONE="Europe/Berlin"
ENABLE_BLOCKLISTS=1
ENABLE_ADGUARD=1
AUTO_INSTALL=1
EOF

    run bash "${IMAGER_COMPAT_SCRIPT}"
    [ "$status" -eq 0 ]

    [ -f "${STATE_DIR}/imager-preseed.json" ]
    grep -q '"AP_SSID": "DropInSSID"' "${STATE_DIR}/imager-preseed.json"
    grep -q '"AP_PASS": "DropInPass123"' "${STATE_DIR}/imager-preseed.json"
    grep -q '"COUNTRY": "DE"' "${STATE_DIR}/imager-preseed.json"
    grep -q '"ENABLE_BLOCKLISTS": "1"' "${STATE_DIR}/imager-preseed.json"
    grep -q '"ENABLE_ADGUARD": "1"' "${STATE_DIR}/imager-preseed.json"
    grep -q '"AUTO_INSTALL": "1"' "${STATE_DIR}/imager-preseed.json"
}

