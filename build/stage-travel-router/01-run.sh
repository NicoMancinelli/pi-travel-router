#!/bin/bash
set -euo pipefail

# Customize the rootfs for the pi-travel-router image.
# Inputs (env): REPO_URL, GIT_REF (defaults provided).

REPO_URL="${REPO_URL:-https://github.com/NicoMancinelli/pi-travel-router.git}"
GIT_REF="${GIT_REF:-main}"
TARGET_DIR="${ROOTFS_DIR}/opt/pi-travel-router"
REPO_STAGE_DIR="$(dirname "$0")"

echo "Cloning ${REPO_URL} @ ${GIT_REF} into ${TARGET_DIR}"
rm -rf "${TARGET_DIR}"
mkdir -p "${TARGET_DIR}"
# C12: use --depth=50 for faster clones; retry checkout loop handles CDN propagation delays.
git clone --depth=50 "${REPO_URL}" "${TARGET_DIR}"
for attempt in 1 2 3 4 5; do
    git -C "${TARGET_DIR}" fetch --depth=1 origin "${GIT_REF}" 2>/dev/null && \
    git -C "${TARGET_DIR}" checkout FETCH_HEAD && break
    [ "$attempt" -lt 5 ] && { echo "Checkout attempt $attempt failed, retrying in 15s..."; sleep 15; } || \
    { echo "ERROR: Could not checkout ${GIT_REF} after 5 attempts" >&2; exit 1; }
done

GIT_SHA="$(git -C "${TARGET_DIR}" rev-parse --short HEAD)"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Install firstboot.service systemd unit if present in the repo.
FIRSTBOOT_UNIT_SRC="${TARGET_DIR}/firstboot/firstboot.service"
FIRSTBOOT_UNIT_DST="${ROOTFS_DIR}/etc/systemd/system/firstboot.service"
if [ -f "${FIRSTBOOT_UNIT_SRC}" ]; then
    install -D -m 0644 "${FIRSTBOOT_UNIT_SRC}" "${FIRSTBOOT_UNIT_DST}"
    on_chroot << 'EOF'
systemctl enable firstboot.service
EOF
else
    echo "WARNING: ${FIRSTBOOT_UNIT_SRC} not found in repo; skipping firstboot enable."
fi

# Hostname.
echo "travelrouter" > "${ROOTFS_DIR}/etc/hostname"
if grep -qE '^127\.0\.1\.1' "${ROOTFS_DIR}/etc/hosts"; then
    sed -i 's/^127\.0\.1\.1.*/127.0.1.1\ttravelrouter/' "${ROOTFS_DIR}/etc/hosts"
else
    printf '127.0.1.1\ttravelrouter\n' >> "${ROOTFS_DIR}/etc/hosts"
fi

# Use root as the only login user. Set a random temporary password (written to
# /boot/firmware/root-password.txt so the user can read it on first boot),
# enable root SSH via key only, and remove the throwaway pi-gen FIRST_USER.
on_chroot << 'EOF'
ROOTPW=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)
echo "root:${ROOTPW}" | chpasswd
echo "TEMP ROOT PASSWORD (change after first login): ${ROOTPW}" > /boot/firmware/root-password.txt 2>/dev/null || echo "${ROOTPW}" > /boot/root-password.txt
mkdir -p /etc/ssh/sshd_config.d
printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\n' \
    > /etc/ssh/sshd_config.d/00-permit-root.conf
chmod 0644 /etc/ssh/sshd_config.d/00-permit-root.conf
# Remove the pi-gen first user (FIRST_USER_NAME=pi) — root is the only account.
if id pi >/dev/null 2>&1; then
    # Randomise pi password before deletion so even if deluser fails the password is unknown
    echo "pi:$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)" | chpasswd 2>/dev/null || true
    pkill -u pi 2>/dev/null || true
    deluser --remove-home pi 2>/dev/null || userdel -r pi 2>/dev/null || true
fi
EOF
# B-H6: Assert pi user was actually removed.
on_chroot << 'EOF'
id pi >/dev/null 2>&1 && { echo "ERROR: pi user still exists after deletion attempt"; exit 1; } || true
EOF

# Install a login-shell banner that warns the user the router is not yet configured.
# The banner is suppressed once firstboot-done sentinel exists.
on_chroot << 'EOF'
cat > /etc/profile.d/00-firstboot-warning.sh << 'WARN'
#!/bin/sh
# Sourced by every interactive login shell via /etc/profile.d/
[ -f /var/lib/travel-router/firstboot-done ] && return 0
RED='\033[1;31m'
YEL='\033[1;33m'
BLD='\033[1m'
RST='\033[0m'
printf "\n"
printf "${RED}##################################################################${RST}\n"
printf "${RED}##${RST}                                                              ${RED}##${RST}\n"
printf "${RED}##${RST}  ${YEL}WARNING: THIS ROUTER IS NOT CONFIGURED YET${RST}               ${RED}##${RST}\n"
printf "${RED}##${RST}                                                              ${RED}##${RST}\n"
printf "${RED}##${RST}  ${BLD}Root password is on the boot partition:${RST}                  ${RED}##${RST}\n"
printf "${RED}##${RST}  ${BLD}  /boot/firmware/root-password.txt${RST}                       ${RED}##${RST}\n"
printf "${RED}##${RST}  ${BLD}Change it NOW or run the setup wizard first.${RST}             ${RED}##${RST}\n"
printf "${RED}##${RST}                                                              ${RED}##${RST}\n"
printf "${RED}##${RST}  ${BLD}Run the setup wizard:${RST}                                    ${RED}##${RST}\n"
printf "${RED}##${RST}    http://192.168.7.1          (USB gadget)                  ${RED}##${RST}\n"
printf "${RED}##${RST}    http://travelrouter.local   (Wi-Fi / mDNS)               ${RED}##${RST}\n"
printf "${RED}##${RST}                                                              ${RED}##${RST}\n"
printf "${RED}##${RST}  ${BLD}SSH:${RST} 192.168.7.1 (USB) or travelrouter.local (Wi-Fi)    ${RED}##${RST}\n"
printf "${RED}##${RST}                                                              ${RED}##${RST}\n"
printf "${RED}##################################################################${RST}\n"
printf "\n"
WARN
chmod 0644 /etc/profile.d/00-firstboot-warning.sh
EOF

# Pre-enable USB gadget mode so the firstboot wizard is reachable over USB-C
# before install.sh has run. Pi Zero 2 W has no Ethernet and no AP yet.
CONFIG_TXT="${ROOTFS_DIR}/boot/firmware/config.txt"
if [ ! -f "$CONFIG_TXT" ]; then
    CONFIG_TXT="${ROOTFS_DIR}/boot/config.txt"
fi
# H25: insert dtoverlay under existing [all] section rather than appending a new one.
if ! grep -q "dtoverlay=dwc2" "$CONFIG_TXT" 2>/dev/null; then
    # Insert after existing [all] line, or append if none
    if grep -q "^\[all\]" "$CONFIG_TXT"; then
        sed -i '/^\[all\]/{n;s/$/\ndtoverlay=dwc2,dr_mode=peripheral/}' "$CONFIG_TXT" || \
        echo "dtoverlay=dwc2,dr_mode=peripheral" >> "$CONFIG_TXT"
    else
        printf '\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$CONFIG_TXT"
    fi
fi

# Load dwc2 and g_ncm at boot.
# g_ncm (CDC NCM) is used instead of g_ether (CDC ECM) because Windows 10/11
# ships inbox NCM drivers and enumerates the gadget natively; ECM requires
# manual RNDIS driver installation on Windows.
mkdir -p "${ROOTFS_DIR}/etc/modules-load.d"
echo "dwc2" > "${ROOTFS_DIR}/etc/modules-load.d/dwc2.conf"
echo "g_ncm" > "${ROOTFS_DIR}/etc/modules-load.d/g-ncm.conf"

# cmdline.txt: modules-load ensures dwc2+g_ncm init during early kernel boot,
# before userspace, so the host enumerates the gadget immediately on plug-in.
CMDLINE_TXT="${ROOTFS_DIR}/boot/firmware/cmdline.txt"
if [ ! -f "$CMDLINE_TXT" ]; then
    CMDLINE_TXT="${ROOTFS_DIR}/boot/cmdline.txt"
fi
if [ -f "$CMDLINE_TXT" ] && ! grep -q "modules-load=dwc2" "$CMDLINE_TXT"; then
    sed -i '1s/$/ modules-load=dwc2,g_ncm/' "$CMDLINE_TXT"
    echo "cmdline.txt: appended modules-load=dwc2,g_ncm"
fi

# NetworkManager profile for usb0: static 192.168.7.1/24 with shared mode (built-in DHCP for laptop).
USB0_NM_SRC="${TARGET_DIR}/config/usb0-firstboot.nmconnection"
USB0_NM_DST="${ROOTFS_DIR}/etc/NetworkManager/system-connections/usb0-firstboot.nmconnection"
if [ -f "${USB0_NM_SRC}" ]; then
    install -D -m 0600 "${USB0_NM_SRC}" "${USB0_NM_DST}"
else
    echo "WARNING: ${USB0_NM_SRC} not found in repo; usb0 first-boot profile not installed."
fi

echo "USB gadget mode preloaded -- wizard reachable via http://192.168.7.1 over USB-C"

# Install imager-compat.service — neutralise Raspberry Pi Imager's firstrun.sh
# so it doesn't create a non-root user, break AP mode, or trigger a premature
# reboot. The service runs before firstboot.service and:
#   1. Extracts any SSH public key from firstrun.sh and writes it to /root/.ssh/authorized_keys
#   2. Rewrites firstrun.sh to a minimal safe stub that only removes the
#      systemd.run=... entries from cmdline.txt (standard Imager self-cleanup).
cat > "${ROOTFS_DIR}/etc/systemd/system/imager-compat.service" << 'UNIT'
[Unit]
Description=Raspberry Pi Imager firstrun.sh compatibility shim
Documentation=https://github.com/NicoMancinelli/pi-travel-router
After=local-fs.target
Before=firstboot.service
ConditionPathExists=|/boot/firmware/firstrun.sh
ConditionPathExists=|/boot/firstrun.sh
DefaultDependencies=no

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/imager-compat.sh

[Install]
WantedBy=sysinit.target
UNIT
chmod 0644 "${ROOTFS_DIR}/etc/systemd/system/imager-compat.service"

# H26: install imager-compat.sh from the committed file rather than an inline heredoc.
install -m 0755 "${REPO_STAGE_DIR}/files/imager-compat.sh" "${ROOTFS_DIR}/usr/local/sbin/imager-compat.sh"

on_chroot << 'EOF'
systemctl enable imager-compat.service
EOF

echo "imager-compat.service installed and enabled"

# Create the captive portal hooks directory and install example scripts.
# Scripts in the live directory (/etc/travel-router/portals/) are auto-loaded
# by captive-check.sh when their name matches the current SSID slug.
# Examples are installed to the examples/ subdirectory — they are NOT loaded
# automatically; users copy and customise them for specific hotel networks.
PORTALS_DIR="${ROOTFS_DIR}/etc/travel-router/portals"
PORTALS_EXAMPLES_DIR="${PORTALS_DIR}/examples"
PORTALS_SRC="${TARGET_DIR}/scripts/portals"

install -d -m 0755 "${PORTALS_DIR}"
install -d -m 0755 "${PORTALS_EXAMPLES_DIR}"

if [ -d "${PORTALS_SRC}" ]; then
    for f in "${PORTALS_SRC}"/*.sh; do
        [ -f "$f" ] || continue
        # H29: install portal example scripts as executable (0755, not 0644).
        install -m 0755 "$f" "${PORTALS_EXAMPLES_DIR}/"
    done
    echo "Portal example scripts installed to ${PORTALS_EXAMPLES_DIR}"
else
    echo "WARNING: ${PORTALS_SRC} not found in repo; portal examples not installed."
fi

# Image version stamp.
cat > "${ROOTFS_DIR}/etc/travel-router-image-version" <<EOF
git_sha=${GIT_SHA}
git_ref=${GIT_REF}
build_date=${BUILD_DATE}
repo_url=${REPO_URL}
EOF
chmod 0644 "${ROOTFS_DIR}/etc/travel-router-image-version"

echo "Stage stage-travel-router complete (sha=${GIT_SHA}, date=${BUILD_DATE})"
