#!/bin/bash
# set-doh-resolver.sh — select a DNS-over-HTTPS resolver preset or custom URL
# Usage: set-doh-resolver.sh <preset|custom_url>
# Presets: cloudflare, quad9, nextdns, adguard, system (disable DoH)
# Install to: /usr/local/sbin/set-doh-resolver.sh

set -euo pipefail

DEFAULTS_FILE="/etc/default/travel-router"
RESOLVED_CONF_DIR="/etc/systemd/resolved.conf.d"
RESOLVED_CONF="${RESOLVED_CONF_DIR}/doh.conf"

usage() {
    echo "Usage: $0 <preset|url>" >&2
    echo "  Presets: cloudflare, quad9, nextdns, adguard, system" >&2
    echo "  Custom:  https://your-doh-server.example.com/dns-query" >&2
    exit 1
}

if [[ $# -lt 1 ]]; then
    usage
fi

INPUT="$1"

# Map preset names to DoH URLs
case "${INPUT}" in
    cloudflare)
        DOH_URL="https://1.1.1.1/dns-query"
        ;;
    quad9)
        DOH_URL="https://dns.quad9.net/dns-query"
        ;;
    nextdns)
        DOH_URL="https://dns.nextdns.io/"
        ;;
    adguard)
        DOH_URL="https://dns.adguard-dns.com/dns-query"
        ;;
    system)
        DOH_URL="system"
        ;;
    https://*)
        DOH_URL="${INPUT}"
        ;;
    *)
        echo "Error: unknown preset '${INPUT}'. Must be cloudflare, quad9, nextdns, adguard, system, or a https:// URL." >&2
        usage
        ;;
esac

# ── Update /etc/default/travel-router ────────────────────────────────────────
if [[ -f "${DEFAULTS_FILE}" ]]; then
    # Use tmp file + mv to avoid sponge dependency
    TMP_DEFAULTS="$(mktemp "${DEFAULTS_FILE}.XXXXXX")"
    grep -v '^DOH_RESOLVER=' "${DEFAULTS_FILE}" > "${TMP_DEFAULTS}" || true
    echo "DOH_RESOLVER=${DOH_URL}" >> "${TMP_DEFAULTS}"
    mv "${TMP_DEFAULTS}" "${DEFAULTS_FILE}"
    echo "Updated ${DEFAULTS_FILE}: DOH_RESOLVER=${DOH_URL}"
else
    echo "DOH_RESOLVER=${DOH_URL}" >> "${DEFAULTS_FILE}"
    echo "Created entry in ${DEFAULTS_FILE}: DOH_RESOLVER=${DOH_URL}"
fi

# ── Handle systemd-resolved integration ──────────────────────────────────────
if systemctl is-active systemd-resolved >/dev/null 2>&1; then
    if [[ "${DOH_URL}" == "system" ]]; then
        # Remove DoH override, restore DHCP-managed DNS
        if [[ -f "${RESOLVED_CONF}" ]]; then
            rm -f "${RESOLVED_CONF}"
            echo "Removed ${RESOLVED_CONF} — systemd-resolved will use DHCP DNS"
        fi
    else
        # Extract the IP address from the DoH URL for the DNS= line
        # Strip scheme and path: https://1.1.1.1/dns-query → 1.1.1.1
        DNS_HOST="$(printf '%s' "${DOH_URL}" | sed 's|^https\?://||; s|/.*||')"

        mkdir -p "${RESOLVED_CONF_DIR}"
        cat > "${RESOLVED_CONF}" <<EOF
# Managed by set-doh-resolver.sh — do not edit manually
[Resolve]
DNS=${DNS_HOST}
DNSOverTLS=yes
EOF
        echo "Wrote ${RESOLVED_CONF}: DNS=${DNS_HOST} DNSOverTLS=yes"
    fi

    systemctl restart systemd-resolved
    echo "Restarted systemd-resolved"
else
    echo "systemd-resolved is not active — skipping resolved.conf update"
fi

echo "DoH resolver set to: ${DOH_URL}"
