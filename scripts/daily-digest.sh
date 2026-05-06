#!/bin/bash
# Daily digest push: uptime, uplink, Tailscale, data transferred, any failed units.

set -euo pipefail
# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

[ -n "${NTFY_TOPIC:-}" ] || exit 0
[ -x /usr/local/bin/notify-router.sh ] || exit 0

# Uptime
uptime_str=$(uptime -p 2>/dev/null | sed 's/up //' || printf "unknown")

# M5/M29: read uplink state file first; fall back to routing table if absent
uplink=""
if [ -f /var/lib/travel-router/uplink.state ]; then
    uplink=$(cat /var/lib/travel-router/uplink.state 2>/dev/null || true)
fi
if [ -z "$uplink" ]; then
    uplink=$(ip route show default 2>/dev/null \
        | awk '/default/{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1);exit}}}' \
        | head -1 || printf "none")
fi
[ -z "$uplink" ] && uplink="none"
case "$uplink" in
    enx*) utype="iPhone USB" ;; rndis0|usb0) utype="Android USB" ;;
    bnep0) utype="BT PAN" ;; wlan0) utype="WiFi" ;;
    tailscale0) utype="Tailscale" ;; *) utype="$uplink" ;;
esac

# Tailscale state
ts_state=$(tailscale status --self 2>/dev/null | awk 'NR==1{print $1}' || printf "unknown")

# Data transferred today (vnstat)
data_today=$(vnstat -i wlan0 --oneline 2>/dev/null | cut -d';' -f11 || printf "n/a")

# Failed systemd units
failed=$(systemctl list-units --state=failed --no-legend 2>/dev/null \
    | awk '{print $1}' | tr '\n' ' ' | sed 's/ $//' || printf "none")
[ -z "$failed" ] && failed="none"

# AP clients right now
clients=$(iw dev uap0 station dump 2>/dev/null | grep -c "^Station" || printf "0")

msg="Daily digest | Up: ${uptime_str} | WAN: ${utype} | TS: ${ts_state} | AP clients: ${clients} | Data: ${data_today} | Failed units: ${failed}"

/usr/local/bin/notify-router.sh "$msg" low
