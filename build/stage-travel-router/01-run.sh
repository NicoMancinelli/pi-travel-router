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

# Image version stamp.
cat > "${ROOTFS_DIR}/etc/travel-router-image-version" <<EOF
git_sha=${GIT_SHA}
git_ref=${GIT_REF}
build_date=${BUILD_DATE}
repo_url=${REPO_URL}
EOF
chmod 0644 "${ROOTFS_DIR}/etc/travel-router-image-version"

echo "Stage stage-travel-router complete (sha=${GIT_SHA}, date=${BUILD_DATE})"
