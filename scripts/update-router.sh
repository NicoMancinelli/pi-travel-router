#!/bin/bash
# Auto-update travel router scripts from the latest GitHub release.
# Runs weekly via update-router.timer. Safe to run manually at any time.
#
# What it updates:  scripts → /usr/local/bin/, systemd units, config templates
# What it never touches: /etc/default/travel-router, /etc/hostapd/hostapd.conf,
#                         /etc/dnsmasq.d/, /etc/iptables/  (user-configured)

set -euo pipefail

REPO="NicoMancinelli/pi-travel-router"
VERSION_FILE="/etc/travel-router-version"
LOG="/var/log/update-router.log"
LOGFILE="$LOG"

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

log()    { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOGFILE"; }
notify() { /usr/local/bin/notify-router.sh "$1" "${2:-default}" 2>/dev/null || true; }

# ── Version helpers ──────────────────────────────────────────────────────────

current_version() {
    cat "$VERSION_FILE" 2>/dev/null || echo "unknown"
}

# Query GitHub for the latest release tag; falls back to latest commit SHA on main
latest_version() {
    local tag
    tag=$(curl -sf --max-time 10 \
        "https://api.github.com/repos/${REPO}/releases/latest" \
        2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tag_name'])" 2>/dev/null || true)

    if [ -n "$tag" ]; then
        echo "$tag"
        return
    fi

    # No releases yet — use the latest commit SHA on main
    curl -sf --max-time 10 \
        "https://api.github.com/repos/${REPO}/commits/main" \
        2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['sha'][:12])" 2>/dev/null || true
}

# ── Download + extract ───────────────────────────────────────────────────────

download_release() {
    local version="$1" tmpdir="$2"
    local url

    # Try release tarball first; fall back to branch archive for commit-based versions
    if [[ "$version" =~ ^v?[0-9] ]]; then
        url="https://github.com/${REPO}/archive/refs/tags/${version}.tar.gz"
    else
        url="https://github.com/${REPO}/archive/refs/heads/main.tar.gz"
    fi

    log "Downloading $url"
    curl -sfL --max-time 120 "$url" -o "${tmpdir}/release.tar.gz" || {
        log "Download failed"
        return 1
    }
    tar -xzf "${tmpdir}/release.tar.gz" -C "$tmpdir" --strip-components=1
}

# ── Apply update ─────────────────────────────────────────────────────────────

apply_update() {
    local src="$1"   # extracted repo root
    local changed=0

    # Scripts → /usr/local/bin/
    for script in "${src}"/scripts/*.sh; do
        name=$(basename "$script")
        dest="/usr/local/bin/${name}"
        if ! diff -q "$script" "$dest" >/dev/null 2>&1; then
            cp "$script" "$dest"
            chmod 755 "$dest"
            log "  updated script: $name"
            changed=1
        fi
    done

    # Systemd units (service + timer files)
    local reload_needed=0
    for unit in "${src}"/systemd/*.service "${src}"/systemd/*.timer; do
        name=$(basename "$unit")
        dest="/etc/systemd/system/${name}"
        if ! diff -q "$unit" "$dest" >/dev/null 2>&1; then
            cp "$unit" "$dest"
            log "  updated unit: $name"
            reload_needed=1
            changed=1
        fi
    done

    if [ "$reload_needed" = "1" ]; then
        systemctl daemon-reload
        log "  systemd daemon reloaded"
    fi

    # install.sh (for reference; never auto-executed)
    if ! diff -q "${src}/install.sh" /usr/local/share/travel-router/install.sh >/dev/null 2>&1; then
        mkdir -p /usr/local/share/travel-router
        cp "${src}/install.sh" /usr/local/share/travel-router/install.sh
        chmod 755 /usr/local/share/travel-router/install.sh
        log "  updated install.sh (at /usr/local/share/travel-router/install.sh — not auto-run)"
        changed=1
    fi

    return $changed
}

# ── Main ─────────────────────────────────────────────────────────────────────

log "=== update-router.sh start ==="

current=$(current_version)
log "Current version: $current"

latest=$(latest_version)
if [ -z "$latest" ]; then
    log "Could not determine latest version (no network or API error) — skipping"
    exit 0
fi
log "Latest version:  $latest"

if [ "$current" = "$latest" ]; then
    log "Already up to date"
    exit 0
fi

log "Update available: $current → $latest"

tmpdir=$(mktemp -d)
# shellcheck disable=SC2064
trap "rm -rf '$tmpdir'" EXIT

if ! download_release "$latest" "$tmpdir"; then
    log "Update aborted: download failed"
    notify "Router update failed (download error) — still on $current" high
    exit 1
fi

if apply_update "$tmpdir"; then
    echo "$latest" > "$VERSION_FILE"
    log "Update complete: $current → $latest"
    notify "Router updated: $current → $latest" low
else
    log "No files changed (already at $latest content)"
    echo "$latest" > "$VERSION_FILE"
fi
