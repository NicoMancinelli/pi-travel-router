#!/bin/bash
# Collect a redacted diagnostic bundle for bug reports.
# Usage: sudo travel-diagnostic [output-dir]
#   output-dir  Optional directory to write the .tar.gz into (default: /tmp)
set -euo pipefail

if [[ "${1:-}" = "--help" || "${1:-}" = "-h" ]]; then
    printf 'Usage: sudo travel-diagnostic [output-dir]\n'
    printf '  Collects logs, network state, and config into a timestamped tar.gz.\n'
    printf '  Secrets are redacted. Share the archive when reporting issues.\n'
    exit 0
fi

[[ $EUID -ne 0 ]] && { echo "Run as root: sudo travel-diagnostic" >&2; exit 1; }

OUTPUT_DIR="${1:-/tmp}"

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
OUTFILE="${OUTPUT_DIR}/travel-diagnostic-${TIMESTAMP}.tar.gz"
DIAG_DIR=$(mktemp -d /tmp/travel-diag-XXXXXX)

collect() {
    local name="$1"; shift
    "$@" > "${DIAG_DIR}/${name}" 2>/dev/null || true
}

for svc in wan-watchdog hostapd dnsmasq NetworkManager tailscaled firstboot systemd-networkd; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        journalctl -u "${svc}" -n 200 --no-pager > "${DIAG_DIR}/journal-${svc}.log" 2>/dev/null || true
    fi
done

collect ip-addr.txt              ip addr
collect ip-route.txt             ip route show table all
collect ip-rule.txt              ip rule
collect interfaces-wireless.txt  bash -c 'iw dev 2>/dev/null || iwconfig 2>/dev/null'
collect nftables.txt             nft list ruleset
collect iptables-nat.txt         iptables -t nat -L -n -v
collect tailscale-status.txt     tailscale status
collect df.txt                   df -h
collect free.txt                 free -h
collect systemctl-failed.txt     systemctl list-units --state=failed --no-pager
collect os-release.txt           cat /etc/os-release
collect image-version.txt        cat /etc/travel-router-image-version

if [[ -f /etc/default/travel-router ]]; then
    sed 's/\(AP_PASS\|TS_KEY\|SSH_ADMIN_KEY\|NTFY_TOPIC\|PASSWORD\|SECRET\|TOKEN\)=.*/\1=REDACTED/' \
        /etc/default/travel-router > "${DIAG_DIR}/travel-router-config.txt" 2>/dev/null || true
fi

tar -czf "${OUTFILE}" -C "${DIAG_DIR}" .
rm -rf "${DIAG_DIR}"

echo "Diagnostic saved to: ${OUTFILE}"
