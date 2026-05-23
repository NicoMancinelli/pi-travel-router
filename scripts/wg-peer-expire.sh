#!/bin/bash
# scripts/wg-peer-expire.sh — Remove WireGuard peers past their # expires: date
# Installed by 03-vpn.sh to /usr/local/sbin/wg-peer-expire.sh
# Run daily via wg-peer-expire.timer at 02:00

set -euo pipefail

WG_CONF="/etc/wireguard/wg0.conf"
LOG_DIR="/var/log/travel-router"
LOG_FILE="${LOG_DIR}/wg-expire.log"
NOTIFY_SCRIPT="/usr/local/sbin/notify-router.sh"
TODAY="$(date +%Y-%m-%d)"

# ── Logging ───────────────────────────────────────────────────────────────────

mkdir -p "${LOG_DIR}"

log() {
    local msg="$1"
    echo "$(date '+%Y-%m-%d %H:%M:%S') wg-peer-expire: ${msg}" | tee -a "${LOG_FILE}"
}

notify() {
    local msg="$1"
    if [[ -x "${NOTIFY_SCRIPT}" ]]; then
        "${NOTIFY_SCRIPT}" "WireGuard peer expired" "${msg}" 2>/dev/null || true
    fi
}

# ── Guard ─────────────────────────────────────────────────────────────────────

if [[ ! -f "${WG_CONF}" ]]; then
    log "INFO: ${WG_CONF} not found — nothing to do"
    exit 0
fi

# ── Parse wg0.conf and collect peer blocks ────────────────────────────────────

# Each element: "<pubkey>|<expires>|<start_line>|<end_line>"
declare -a EXPIRED_PEERS=()

in_peer=0
peer_pubkey=""
peer_expires=""
peer_start=0

# Read into arrays so we can reference by line number
mapfile -t CONF_LINES < "${WG_CONF}"
total_lines=${#CONF_LINES[@]}

i=0
while [[ "${i}" -lt "${total_lines}" ]]; do
    line="${CONF_LINES[${i}]}"

    if [[ "${line}" =~ ^\[Peer\] ]]; then
        in_peer=1
        peer_pubkey=""
        peer_expires=""
        peer_start="${i}"
    elif [[ "${in_peer}" -eq 1 ]]; then
        if [[ "${line}" =~ ^PublicKey[[:space:]]*=[[:space:]]*(.+)$ ]]; then
            peer_pubkey="${BASH_REMATCH[1]}"
            # trim trailing whitespace
            peer_pubkey="${peer_pubkey%"${peer_pubkey##*[! ]}"}"
        elif [[ "${line}" =~ ^#[[:space:]]*expires:[[:space:]]*([0-9]{4}-[0-9]{2}-[0-9]{2}) ]]; then
            peer_expires="${BASH_REMATCH[1]}"
        elif [[ -z "${line}" ]] || [[ "${line}" =~ ^\[Interface\] ]] || [[ "${line}" =~ ^\[Peer\] ]]; then
            # End of this peer block
            if [[ -n "${peer_pubkey}" && -n "${peer_expires}" ]]; then
                # peer_end is the last non-blank line of this block (inclusive blank separator)
                peer_end=$(( i - 1 ))
                # Include trailing blank line if present
                if [[ "${i}" -lt "${total_lines}" ]] && [[ -z "${CONF_LINES[${i}]}" ]]; then
                    peer_end="${i}"
                fi
                EXPIRED_PEERS+=("${peer_pubkey}|${peer_expires}|${peer_start}|${peer_end}")
            fi
            in_peer=0
            peer_pubkey=""
            peer_expires=""
            # If we hit another [Peer], don't skip it
            if [[ "${line}" =~ ^\[Peer\] ]]; then
                in_peer=1
                peer_start="${i}"
            fi
        fi
    fi
    (( i++ )) || true
done

# Flush final peer block at EOF
if [[ "${in_peer}" -eq 1 && -n "${peer_pubkey}" && -n "${peer_expires}" ]]; then
    peer_end=$(( total_lines - 1 ))
    EXPIRED_PEERS+=("${peer_pubkey}|${peer_expires}|${peer_start}|${peer_end}")
fi

# ── Filter to only expired peers ──────────────────────────────────────────────

declare -a TO_REMOVE=()
for entry in "${EXPIRED_PEERS[@]}"; do
    IFS='|' read -r key expires _start _end <<< "${entry}"
    if [[ "${expires}" < "${TODAY}" ]] || [[ "${expires}" == "${TODAY}" ]]; then
        TO_REMOVE+=("${entry}")
        log "INFO: peer ${key:0:20}... expired on ${expires} (today is ${TODAY})"
    fi
done

if [[ "${#TO_REMOVE[@]}" -eq 0 ]]; then
    log "INFO: no expired peers found"
    exit 0
fi

# ── Check if wg0 is up ────────────────────────────────────────────────────────

wg0_up=0
if ip link show wg0 > /dev/null 2>&1; then
    wg0_up=1
fi

# ── Remove expired peers from conf (process in reverse line order) ────────────

# Sort by start line descending so removing earlier lines doesn't shift later ones
declare -a SORTED_REMOVE=()
while IFS= read -r line; do
    SORTED_REMOVE+=("${line}")
done < <(printf '%s\n' "${TO_REMOVE[@]}" | sort -t'|' -k3 -rn)

# Work on a copy of the lines array
declare -a NEW_LINES=("${CONF_LINES[@]}")

for entry in "${SORTED_REMOVE[@]}"; do
    IFS='|' read -r key expires start_idx end_idx <<< "${entry}"
    # Delete lines from start_idx to end_idx (0-based) inclusive
    NEW_LINES=("${NEW_LINES[@]:0:${start_idx}}" "${NEW_LINES[@]:$(( end_idx + 1 ))}")
done

# ── Write updated conf atomically ────────────────────────────────────────────

TMP_CONF="$(mktemp /etc/wireguard/wg0.conf.XXXXXX)"
# shellcheck disable=SC2064
trap "rm -f '${TMP_CONF}'" EXIT

printf '%s\n' "${NEW_LINES[@]}" > "${TMP_CONF}"
chmod 600 "${TMP_CONF}"
mv "${TMP_CONF}" "${WG_CONF}"
trap - EXIT

log "INFO: wrote updated ${WG_CONF} (removed ${#TO_REMOVE[@]} peer(s))"

# ── Remove peers live if wg0 is up ───────────────────────────────────────────

removed_keys=()
for entry in "${TO_REMOVE[@]}"; do
    IFS='|' read -r key expires _start _end <<< "${entry}"
    if [[ "${wg0_up}" -eq 1 ]]; then
        if wg set wg0 peer "${key}" remove 2>/dev/null; then
            log "INFO: removed live peer ${key:0:20}... from wg0"
        else
            log "WARN: failed to remove live peer ${key:0:20}... (interface may be gone)"
        fi
    fi
    removed_keys+=("${key:0:20}...")
done

# ── Notify ────────────────────────────────────────────────────────────────────

msg="Removed ${#TO_REMOVE[@]} expired WireGuard peer(s): $(IFS=','; echo "${removed_keys[*]}")"
log "INFO: ${msg}"
notify "${msg}"
