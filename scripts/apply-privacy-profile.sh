#!/bin/bash
# apply-privacy-profile.sh — atomically switch between privacy profiles
# Usage: apply-privacy-profile.sh <profile-name>
# Profiles: vpn-only  adblock-only  tor  direct
set -euo pipefail

# ── Constants ──────────────────────────────────────────────────────────────────
readonly VALID_PROFILES="vpn-only adblock-only tor direct"
readonly PROFILE_DIR_ETC="/etc/travel-router/privacy-profiles"
readonly PROFILE_DIR_SHARE="/usr/local/share/travel-router/privacy-profiles"
readonly STATE_DIR="/var/lib/travel-router"
readonly LOG_DIR="/var/log/travel-router"
readonly LOG_FILE="${LOG_DIR}/privacy-profile.log"
readonly ACTIVE_FILE="${STATE_DIR}/active-profile"
readonly PREV_FILE="${STATE_DIR}/previous-profile"
readonly KS_MARKER="${STATE_DIR}/kill-switch-active"
readonly REVERT_PID_FILE="/var/run/travel-router-profile-revert.pid"
readonly FIREWALL_SCRIPT="/usr/local/sbin/travel-router-firewall.sh"
readonly NOTIFY_SCRIPT="/usr/local/sbin/notify-router.sh"

# ── Logging ────────────────────────────────────────────────────────────────────
_log() {
    local msg="$1"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "${ts}  apply-privacy-profile: ${msg}" | tee -a "${LOG_FILE}" >&2
}

# ── Usage ──────────────────────────────────────────────────────────────────────
usage() {
    echo "Usage: $(basename "$0") <profile-name>" >&2
    echo "Profiles: ${VALID_PROFILES}" >&2
    exit 1
}

# ── Validate profile name ──────────────────────────────────────────────────────
[[ $# -eq 1 ]] || usage
PROFILE_NAME="$1"

valid=false
for p in ${VALID_PROFILES}; do
    if [[ "${PROFILE_NAME}" == "${p}" ]]; then
        valid=true
        break
    fi
done
if [[ "${valid}" != "true" ]]; then
    _log "ERROR: unknown profile '${PROFILE_NAME}'"
    usage
fi

# ── Ensure directories exist ───────────────────────────────────────────────────
mkdir -p "${STATE_DIR}" "${LOG_DIR}"

# ── Locate YAML file ───────────────────────────────────────────────────────────
YAML_FILE=""
if [[ -f "${PROFILE_DIR_ETC}/${PROFILE_NAME}.yaml" ]]; then
    YAML_FILE="${PROFILE_DIR_ETC}/${PROFILE_NAME}.yaml"
elif [[ -f "${PROFILE_DIR_SHARE}/${PROFILE_NAME}.yaml" ]]; then
    YAML_FILE="${PROFILE_DIR_SHARE}/${PROFILE_NAME}.yaml"
else
    _log "ERROR: profile YAML not found for '${PROFILE_NAME}'"
    exit 1
fi

_log "Applying profile '${PROFILE_NAME}' from ${YAML_FILE}"

# ── Parse YAML (simple key: value, no nested structures) ──────────────────────
_yaml_val() {
    local key="$1"
    grep -m1 "^${key}:" "${YAML_FILE}" | sed 's/^[^:]*:[[:space:]]*//' | tr -d '"'"'"
}

VPN="$(_yaml_val vpn)"
ADBLOCK="$(_yaml_val adblock)"
TOR="$(_yaml_val tor)"
KILL_SWITCH="$(_yaml_val kill_switch)"
REVERT_AFTER="$(_yaml_val revert_after)"
DNS="$(_yaml_val dns)"

# firewall_extra is a YAML list; collect indented "  - ..." lines after the key
FIREWALL_EXTRA=()
in_extra=false
while IFS= read -r line; do
    if [[ "${line}" =~ ^firewall_extra: ]]; then
        in_extra=true
        continue
    fi
    if [[ "${in_extra}" == "true" ]]; then
        if [[ "${line}" =~ ^[[:space:]]+-[[:space:]]+(.*) ]]; then
            rule="${BASH_REMATCH[1]}"
            # Strip surrounding quotes if present
            rule="${rule#\"}"
            rule="${rule%\"}"
            FIREWALL_EXTRA+=("${rule}")
        elif [[ "${line}" =~ ^[^[:space:]] ]]; then
            in_extra=false
        fi
    fi
done < "${YAML_FILE}"

# ── Save state ─────────────────────────────────────────────────────────────────
OLD_PROFILE="vpn-only"
if [[ -f "${ACTIVE_FILE}" ]]; then
    OLD_PROFILE="$(cat "${ACTIVE_FILE}")"
fi
echo "${OLD_PROFILE}" > "${PREV_FILE}"
echo "${PROFILE_NAME}" > "${ACTIVE_FILE}"
_log "State saved: previous='${OLD_PROFILE}' active='${PROFILE_NAME}'"

# ── Kill any pending revert timer ──────────────────────────────────────────────
if [[ -f "${REVERT_PID_FILE}" ]]; then
    old_pid="$(cat "${REVERT_PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
        _log "Cancelling previous revert timer (PID ${old_pid})"
        kill "${old_pid}" 2>/dev/null || true
    fi
    rm -f "${REVERT_PID_FILE}"
fi

# ── Step 1: VPN (WireGuard) ────────────────────────────────────────────────────
if [[ "${VPN}" == "true" ]]; then
    _log "Starting WireGuard (wg-quick@wg0)"
    systemctl start wg-quick@wg0 2>/dev/null || true
else
    _log "Stopping WireGuard (wg-quick@wg0)"
    systemctl stop wg-quick@wg0 2>/dev/null || true
fi

# ── Step 2: Tailscale ─────────────────────────────────────────────────────────
if [[ "${VPN}" != "true" ]]; then
    _log "Taking Tailscale down (VPN=false)"
    tailscale down 2>/dev/null || true
fi

# ── Step 2b: AdGuard Home ─────────────────────────────────────────────────────
if [[ "${ADBLOCK}" == "true" ]]; then
    _log "Starting AdGuard Home"
    systemctl start adguardhome 2>/dev/null || true
else
    _log "Stopping AdGuard Home"
    systemctl stop adguardhome 2>/dev/null || true
fi

# ── Step 3: Tor ───────────────────────────────────────────────────────────────
if [[ "${TOR}" == "true" ]]; then
    if ! command -v tor &>/dev/null; then
        _log "WARNING: tor not installed — skipping Tor setup"
    else
        _log "Configuring Tor transparent proxy"
        TORRC="/etc/tor/torrc"
        if [[ -f "${TORRC}" ]]; then
            # Ensure TransPort 9040 is present
            if ! grep -q "^TransPort 9040" "${TORRC}"; then
                echo "TransPort 9040" >> "${TORRC}"
            fi
            # Ensure DNSPort 5353 is present
            if ! grep -q "^DNSPort 5353" "${TORRC}"; then
                echo "DNSPort 5353" >> "${TORRC}"
            fi
        else
            _log "WARNING: ${TORRC} not found — skipping Tor torrc configuration"
        fi
        _log "Restarting Tor"
        systemctl restart tor 2>/dev/null || _log "WARNING: failed to restart tor"
    fi
fi

# ── Step 4: Kill switch ────────────────────────────────────────────────────────
if [[ "${KILL_SWITCH}" == "true" ]]; then
    _log "Activating kill switch"
    touch "${KS_MARKER}"
    if [[ -x "${FIREWALL_SCRIPT}" ]]; then
        "${FIREWALL_SCRIPT}" 2>/dev/null || _log "WARNING: firewall script returned non-zero"
    fi
else
    _log "Deactivating kill switch"
    rm -f "${KS_MARKER}"
fi

# ── Step 5: DNS ───────────────────────────────────────────────────────────────
_log "Configuring DNS: ${DNS}"
RESOLV="/etc/resolv.conf"
RESOLV_BACKUP="/etc/resolv.conf.travel-router-backup"

case "${DNS}" in
    tor)
        # Backup current resolv.conf if backup doesn't exist
        if [[ ! -f "${RESOLV_BACKUP}" ]] && [[ -f "${RESOLV}" ]]; then
            cp "${RESOLV}" "${RESOLV_BACKUP}"
        fi
        # Write Tor DNS (DNSPort 5353 is forwarded by dnsmasq; point resolv.conf to localhost)
        printf "# Managed by apply-privacy-profile.sh (tor)\nnameserver 127.0.0.1\n" > "${RESOLV}"
        ;;
    adguard)
        if [[ ! -f "${RESOLV_BACKUP}" ]] && [[ -f "${RESOLV}" ]]; then
            cp "${RESOLV}" "${RESOLV_BACKUP}"
        fi
        # AdGuard Home runs locally; point resolver to localhost
        printf "# Managed by apply-privacy-profile.sh (adguard)\nnameserver 127.0.0.1\n" > "${RESOLV}"
        ;;
    system)
        if [[ -f "${RESOLV_BACKUP}" ]]; then
            _log "Restoring DNS from backup"
            cp "${RESOLV_BACKUP}" "${RESOLV}"
            rm -f "${RESOLV_BACKUP}"
        else
            _log "No DNS backup found — leaving resolv.conf unchanged"
        fi
        ;;
    *)
        _log "WARNING: unknown dns value '${DNS}' — leaving resolv.conf unchanged"
        ;;
esac

# ── Step 6: Extra firewall rules ───────────────────────────────────────────────
# Always flush travel-router-managed nat PREROUTING rules first
# We identify them by the iptables comment "travel-router-profile"
iptables -t nat -S PREROUTING 2>/dev/null \
    | grep "travel-router-profile" \
    | sed 's/^-A/-D/' \
    | while IFS= read -r rule; do
        # shellcheck disable=SC2086
        iptables -t nat ${rule} 2>/dev/null || true
    done

if [[ ${#FIREWALL_EXTRA[@]} -gt 0 ]]; then
    _log "Applying ${#FIREWALL_EXTRA[@]} extra firewall rules"
    for rule in "${FIREWALL_EXTRA[@]}"; do
        _log "  iptables -t nat ${rule} -m comment --comment travel-router-profile"
        # shellcheck disable=SC2086
        iptables -t nat ${rule} -m comment --comment "travel-router-profile" 2>/dev/null \
            || _log "WARNING: iptables rule failed: ${rule}"
    done
else
    _log "No extra firewall rules to apply"
fi

# ── Step 7: Auto-revert ────────────────────────────────────────────────────────
# Use default 0 if REVERT_AFTER is empty or not numeric
if [[ -z "${REVERT_AFTER}" ]]; then
    REVERT_AFTER=0
fi
if [[ "${REVERT_AFTER}" =~ ^[0-9]+$ ]] && [[ "${REVERT_AFTER}" -gt 0 ]]; then
    _log "Scheduling auto-revert to '${OLD_PROFILE}' in ${REVERT_AFTER}s"
    SCRIPT_PATH="$(readlink -f "$0")"
    (
        sleep "${REVERT_AFTER}"
        revert_to="$(cat "${PREV_FILE}" 2>/dev/null || echo "vpn-only")"
        "${SCRIPT_PATH}" "${revert_to}"
    ) &
    echo "$!" > "${REVERT_PID_FILE}"
    _log "Revert timer PID: $(cat "${REVERT_PID_FILE}")"
fi

# ── Notify ────────────────────────────────────────────────────────────────────
if [[ -x "${NOTIFY_SCRIPT}" ]]; then
    "${NOTIFY_SCRIPT}" "Privacy profile activated: ${PROFILE_NAME}" 2>/dev/null || true
fi

_log "Profile '${PROFILE_NAME}' applied successfully"
exit 0
