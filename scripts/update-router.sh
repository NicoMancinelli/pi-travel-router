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
readonly REPO VERSION_FILE LOG LOGFILE

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

log()    { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$LOGFILE"; }
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
    local url tarball="${tmpdir}/release.tar.gz"

    # Try release tarball first; fall back to branch archive for commit-based versions
    if [[ "$version" =~ ^v?[0-9] ]]; then
        url="https://github.com/${REPO}/archive/refs/tags/${version}.tar.gz"
    else
        url="https://github.com/${REPO}/archive/refs/heads/main.tar.gz"
    fi

    log "Downloading $url"
    curl -sfL --max-time 120 "$url" -o "$tarball" || {
        log "Download failed"
        return 1
    }

    # H6: integrity check — verify the tarball is a valid tar archive
    tar -tjf "$tarball" >/dev/null 2>&1 || {
        log "Downloaded tarball is corrupt"
        return 1
    }
    log "Tarball integrity OK"

    tar -xzf "$tarball" -C "$tmpdir" --strip-components=1
}

# ── Apply update ─────────────────────────────────────────────────────────────

apply_update() {
    local src="$1"   # extracted repo root

    # T-H8: explicit allowlist — only install scripts whose names are known-good.
    # This prevents a compromised tarball from installing arbitrary executables.
    SCRIPT_ALLOWLIST=(
        failover-watchdog.sh wan-watchdog.sh captive-check.sh travel-router-firewall.sh
        apply-split-tunnel.sh start-tether.sh start-bt-tether.sh stop-bt-tether.sh
        stop-tether.sh clone-mac.sh ap-schedule.sh tailscale-watchdog.sh ups-monitor.sh notify-router.sh
        travel-tui.sh travel-status.sh travel-diagnostic.sh generate-bandwidth-report.sh
        vnstat-push.sh vnstat-metrics.sh tune-cake.sh daily-digest.sh update-router.sh update-blocklists.sh
        setup-2fa.sh install-adguard.sh apply-cake.sh
    )

    # Scripts → /usr/local/bin/
    shopt -s nullglob
    for script in "${src}"/scripts/*.sh; do
        name=$(basename "$script")

        # Skip any script not in the allowlist
        local _allowed=0
        for _a in "${SCRIPT_ALLOWLIST[@]}"; do
            [[ "$_a" = "$name" ]] && { _allowed=1; break; }
        done
        if [[ "$_allowed" -eq 0 ]]; then
            log "  SKIP (not in allowlist): $name"
            continue
        fi

        # travel-diagnostic is installed without the .sh extension
        if [[ "$name" = "travel-diagnostic.sh" ]]; then
            dest="/usr/local/bin/travel-diagnostic"
        else
            dest="/usr/local/bin/${name}"
        fi
        if ! diff -q "$script" "$dest" >/dev/null 2>&1; then
            # C5: atomic write — copy to .tmp then rename so running scripts are not
            # overwritten in-place while executing
            cp "$script" "${dest}.tmp" && mv "${dest}.tmp" "$dest"
            chmod 755 "$dest"
            log "  updated script: $name"
            changed=1
        fi
    done
    shopt -u nullglob

    # H7: portal example scripts → /etc/travel-router/portals/examples/
    PORTAL_ALLOWLIST=(example-accept-terms.sh example-credentials.sh)
    if [ -d "${src}/scripts/portals" ]; then
        mkdir -p /etc/travel-router/portals/examples
        shopt -s nullglob
        for portal in "${src}"/scripts/portals/*.sh; do
            [ -f "$portal" ] || continue
            pname=$(basename "$portal")
            # Skip portal scripts not in the allowlist
            local _pallowed=0
            for _pa in "${PORTAL_ALLOWLIST[@]}"; do
                [[ "$_pa" = "$pname" ]] && { _pallowed=1; break; }
            done
            if [[ "$_pallowed" -eq 0 ]]; then
                log "  SKIP portal (not in allowlist): $pname"
                continue
            fi
            pdest="/etc/travel-router/portals/examples/${pname}"
            if ! diff -q "$portal" "$pdest" >/dev/null 2>&1; then
                cp "$portal" "${pdest}.tmp" && mv "${pdest}.tmp" "$pdest"
                chmod 755 "$pdest"
                log "  updated portal example: $pname"
                changed=1
            fi
        done
        shopt -u nullglob
    fi

    # Systemd units (service + timer files)
    local reload_needed=0
    shopt -s nullglob
    for unit in "${src}"/systemd/*.service "${src}"/systemd/*.timer; do
        name=$(basename "$unit")
        dest="/etc/systemd/system/${name}"
        if ! diff -q "$unit" "$dest" >/dev/null 2>&1; then
            cp "$unit" "${dest}.tmp" && mv "${dest}.tmp" "$dest"
            log "  updated unit: $name"
            reload_needed=1
            changed=1
        fi
    done
    shopt -u nullglob

    if [ "$reload_needed" = "1" ]; then
        systemctl daemon-reload
        log "  systemd daemon reloaded"
    fi

    # Re-apply firewall if the firewall script changed (picks up new nftables rules)
    if ! diff -q "${src}/scripts/travel-router-firewall.sh" /usr/local/bin/travel-router-firewall.sh >/dev/null 2>&1; then
        if /usr/local/bin/travel-router-firewall.sh --save 2>/dev/null; then
            log "  firewall rules reloaded"
        else
            log "  WARNING: firewall reload failed — rules unchanged"
        fi
    fi

    # install.sh (for reference; never auto-executed)
    if ! diff -q "${src}/install.sh" /usr/local/share/travel-router/install.sh >/dev/null 2>&1; then
        mkdir -p /usr/local/share/travel-router
        cp "${src}/install.sh" /usr/local/share/travel-router/install.sh.tmp \
            && mv /usr/local/share/travel-router/install.sh.tmp \
                  /usr/local/share/travel-router/install.sh
        chmod 755 /usr/local/share/travel-router/install.sh
        log "  updated install.sh (at /usr/local/share/travel-router/install.sh — not auto-run)"
        changed=1
    fi

    return 0
}

# ── Main ─────────────────────────────────────────────────────────────────────

log "=== update-router.sh start ==="

current=$(current_version)
log "Current version: $current"

latest=$(latest_version | tr -cd 'A-Za-z0-9._-' | head -c 40)
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
trap "rm -rf '$tmpdir'" EXIT INT TERM

if ! download_release "$latest" "$tmpdir"; then
    log "Update aborted: download failed"
    notify "Router update failed (download error) — still on $current" high
    exit 1
fi

changed=0
apply_update "$tmpdir"
if [[ "$changed" = "1" ]]; then
    echo "$latest" > "$VERSION_FILE"
    log "Update complete: $current → $latest"
    notify "Router updated: $current → $latest" low
else
    log "No files changed (already at $latest content)"
    echo "$latest" > "$VERSION_FILE"
fi
