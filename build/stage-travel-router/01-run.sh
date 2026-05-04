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

# Image version stamp.
cat > "${ROOTFS_DIR}/etc/travel-router-image-version" <<EOF
git_sha=${GIT_SHA}
git_ref=${GIT_REF}
build_date=${BUILD_DATE}
repo_url=${REPO_URL}
EOF
chmod 0644 "${ROOTFS_DIR}/etc/travel-router-image-version"

echo "Stage stage-travel-router complete (sha=${GIT_SHA}, date=${BUILD_DATE})"
