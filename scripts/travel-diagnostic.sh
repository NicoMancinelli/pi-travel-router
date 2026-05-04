#!/bin/bash
set -euo pipefail

[[ $EUID -ne 0 ]] && { echo "Run as root: sudo travel-diagnostic" >&2; exit 1; }

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
OUTFILE="/tmp/travel-diagnostic-${TIMESTAMP}.tar.gz"
TMPDIR=$(mktemp -d /tmp/travel-diag-XXXXXX)

collect() {
    local name="$1"; shift
    "$@" > "${TMPDIR}/${name}" 2>/dev/null || true
}

for svc in wan-watchdog hostapd dnsmasq NetworkManager tailscaled firstboot systemd-networkd; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        journalctl -u "${svc}" -n 200 --no-pager > "${TMPDIR}/journal-${svc}.log" 2>/dev/null || true
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
        /etc/default/travel-router > "${TMPDIR}/travel-router-config.txt" 2>/dev/null || true
fi

tar -czf "${OUTFILE}" -C "${TMPDIR}" .
rm -rf "${TMPDIR}"

echo "Diagnostic saved to: ${OUTFILE}"
