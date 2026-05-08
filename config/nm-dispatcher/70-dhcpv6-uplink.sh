#!/bin/bash
# NetworkManager dispatcher: start/stop DHCPv6 on uplink (WAN) interfaces.
# Installed to /etc/NetworkManager/dispatcher.d/70-dhcpv6-uplink by install.sh.
# Only runs on WAN interfaces — never on uap0 (AP) or lo.
#
# NetworkManager passes $1=IFACE $2=ACTION to all dispatcher scripts.

IFACE="$1"
ACTION="$2"

# Guard: only act on uplink interfaces
case "${IFACE}" in
    uap0|lo) exit 0 ;;
esac

PIDFILE="/run/dhclient6-${IFACE}.pid"
CFGFILE="/etc/dhclient6.conf"

case "${ACTION}" in
    up)
        # Start DHCPv6 client if not already running for this interface
        if [ -f "${PIDFILE}" ] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
            exit 0
        fi
        dhclient -6 \
            -cf "${CFGFILE}" \
            -pf "${PIDFILE}" \
            -lf "/var/lib/dhcp/dhclient6-${IFACE}.leases" \
            "${IFACE}" &
        ;;
    down|pre-down)
        # Release DHCPv6 lease and stop the client
        if [ -f "${PIDFILE}" ]; then
            dhclient -6 -r -pf "${PIDFILE}" "${IFACE}" 2>/dev/null || true
            rm -f "${PIDFILE}"
        fi
        ;;
esac

exit 0
