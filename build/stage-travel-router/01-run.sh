#!/bin/bash -e
set -euo pipefail

# Customize the rootfs for the pi-travel-router image.
# Inputs (env): REPO_URL, GIT_REF (defaults provided).

REPO_URL="${REPO_URL:-https://github.com/NicoMancinelli/pi-travel-router.git}"
GIT_REF="${GIT_REF:-main}"
TARGET_DIR="${ROOTFS_DIR}/opt/pi-travel-router"

echo "Cloning ${REPO_URL} @ ${GIT_REF} into ${TARGET_DIR}"
rm -rf "${TARGET_DIR}"
mkdir -p "${TARGET_DIR}"
git clone "${REPO_URL}" "${TARGET_DIR}"
git -C "${TARGET_DIR}" checkout "${GIT_REF}"

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

# Use root as the only login user. Set password to 'changeme' (user must change),
# enable root SSH login, and remove the throwaway pi-gen FIRST_USER.
on_chroot << 'EOF'
echo 'root:changeme' | chpasswd
mkdir -p /etc/ssh/sshd_config.d
printf 'PermitRootLogin yes\nPasswordAuthentication yes\n' \
    > /etc/ssh/sshd_config.d/00-permit-root.conf
chmod 0644 /etc/ssh/sshd_config.d/00-permit-root.conf
# Remove the pi-gen first user (FIRST_USER_NAME) — root is the only account.
if id neek >/dev/null 2>&1; then
    pkill -u neek 2>/dev/null || true
    deluser --remove-home neek 2>/dev/null || userdel -r neek 2>/dev/null || true
fi
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
printf "${RED}##${RST}  ${BLD}Root password is the factory default:${RST}                    ${RED}##${RST}\n"
printf "${RED}##${RST}  ${BLD}  changeme${RST}                                               ${RED}##${RST}\n"
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
if ! grep -q "dtoverlay=dwc2" "$CONFIG_TXT" 2>/dev/null; then
    {
        echo ""
        echo "# pi-travel-router: USB gadget mode for first-boot wizard reachability"
        echo "[all]"
        echo "dtoverlay=dwc2,dr_mode=peripheral"
    } >> "$CONFIG_TXT"
fi

# Load dwc2 and g_ether at boot.
mkdir -p "${ROOTFS_DIR}/etc/modules-load.d"
echo "dwc2" > "${ROOTFS_DIR}/etc/modules-load.d/dwc2.conf"
echo "g_ether" > "${ROOTFS_DIR}/etc/modules-load.d/g-ether.conf"

# cmdline.txt: modules-load ensures dwc2+g_ether init during early kernel boot,
# before userspace, so the host enumerates the gadget immediately on plug-in.
CMDLINE_TXT="${ROOTFS_DIR}/boot/firmware/cmdline.txt"
if [ ! -f "$CMDLINE_TXT" ]; then
    CMDLINE_TXT="${ROOTFS_DIR}/boot/cmdline.txt"
fi
if [ -f "$CMDLINE_TXT" ] && ! grep -q "modules-load=dwc2" "$CMDLINE_TXT"; then
    sed -i '1s/$/ modules-load=dwc2,g_ether/' "$CMDLINE_TXT"
    echo "cmdline.txt: appended modules-load=dwc2,g_ether"
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

cat > "${ROOTFS_DIR}/usr/local/sbin/imager-compat.sh" << 'SCRIPT'
#!/bin/bash
# imager-compat.sh — neutralise Raspberry Pi Imager firstrun.sh
set -euo pipefail

FIRSTRUN=""
for candidate in /boot/firmware/firstrun.sh /boot/firstrun.sh; do
    if [ -f "$candidate" ]; then
        FIRSTRUN="$candidate"
        break
    fi
done

[ -z "$FIRSTRUN" ] && exit 0

# Only act if this looks like an Imager-generated script.
if ! grep -qE 'systemd\.run|raspi-config|authorized_keys' "$FIRSTRUN" 2>/dev/null; then
    exit 0
fi

# Extract SSH public key(s) and write to /root/.ssh/authorized_keys.
PUBKEY=""
while IFS= read -r line; do
    if echo "$line" | grep -qE '(authorized_keys|echo.*ssh-)'; then
        KEY=$(echo "$line" | grep -oE '(ssh-(rsa|ed25519|dss)|ecdsa-sha2-[^ ]+) [A-Za-z0-9+/=]+ ?[^ ]*' | head -1)
        if [ -n "$KEY" ]; then
            PUBKEY="$KEY"
            break
        fi
    fi
done < "$FIRSTRUN"

if [ -n "$PUBKEY" ]; then
    mkdir -p /root/.ssh
    chmod 0700 /root/.ssh
    AK=/root/.ssh/authorized_keys
    touch "$AK"
    if ! grep -qF "$PUBKEY" "$AK" 2>/dev/null; then
        echo "$PUBKEY" >> "$AK"
    fi
    chmod 0600 "$AK"
    chown -R root:root /root/.ssh
fi

# Rewrite firstrun.sh to a minimal safe stub — only the cmdline.txt cleanup
# that the systemd.run= boot mechanism expects.
cat > "$FIRSTRUN" << 'STUB'
#!/bin/bash
# Neutralised by pi-travel-router imager-compat: SSH key already applied to root.
# Remove systemd.run entries from cmdline.txt so this doesn't re-run.
if [ -f /boot/firmware/cmdline.txt ]; then
    sed -i 's| systemd\.run=[^ ]*||g; s| systemd\.run_success_action=[^ ]*||g; s| systemd\.unit=kernel-command-line\.target||g' /boot/firmware/cmdline.txt
fi
if [ -f /boot/cmdline.txt ]; then
    sed -i 's| systemd\.run=[^ ]*||g; s| systemd\.run_success_action=[^ ]*||g; s| systemd\.unit=kernel-command-line\.target||g' /boot/cmdline.txt
fi
STUB
chmod 0755 "$FIRSTRUN"

exit 0
SCRIPT
chmod 0755 "${ROOTFS_DIR}/usr/local/sbin/imager-compat.sh"

on_chroot << 'EOF'
systemctl enable imager-compat.service
EOF

echo "imager-compat.service installed and enabled"

# Image version stamp.
cat > "${ROOTFS_DIR}/etc/travel-router-image-version" <<EOF
git_sha=${GIT_SHA}
git_ref=${GIT_REF}
build_date=${BUILD_DATE}
repo_url=${REPO_URL}
EOF
chmod 0644 "${ROOTFS_DIR}/etc/travel-router-image-version"

echo "Stage stage-travel-router complete (sha=${GIT_SHA}, date=${BUILD_DATE})"
