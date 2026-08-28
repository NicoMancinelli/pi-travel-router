#!/bin/bash
# Daily digest push: uptime, uplink, Tailscale, data transferred, any failed units.

set -euo pipefail
# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

AP_IFACE="${AP_IFACE:-uap0}"

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

# Data transferred today (vnstat) — parse JSON to avoid version-dependent field indices
data_today=$(python3 -c "
import json, subprocess, sys
try:
    d = json.loads(subprocess.check_output(['vnstat', '--json', 'd', '1', '-i', 'wlan0']))
    t = d['interfaces'][0]['traffic']['day']
    rx = t[-1]['rx'] if t else 0
    tx = t[-1]['tx'] if t else 0
    def fmt(b):
        if b >= 1073741824:
            return '%.1f GiB' % (b / 1073741824)
        elif b >= 1048576:
            return '%.1f MiB' % (b / 1048576)
        elif b >= 1024:
            return '%d KiB' % (b // 1024)
        return '%d B' % b
    print('rx %s / tx %s' % (fmt(rx), fmt(tx)))
except Exception:
    print('n/a')
" 2>/dev/null || printf "n/a")

# Failed systemd units
failed=$(systemctl list-units --state=failed --no-legend 2>/dev/null \
    | awk '{print $1}' | tr '\n' ' ' | sed 's/ $//' || printf "none")
[ -z "$failed" ] && failed="none"

# AP clients right now
clients=$(iw dev "${AP_IFACE}" station dump 2>/dev/null | grep -c "^Station" || printf "0")

# CPU Temp & Throttling
temp_str="n/a"
if [ -f /sys/class/thermal/thermal_zone0/temp ]; then
    raw_temp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo "0")
    if [ "$raw_temp" -gt 0 ]; then
        temp_str="$(( raw_temp / 1000 ))°C"
    fi
fi

power_str="OK"
if command -v vcgencmd >/dev/null 2>&1; then
    raw_throttled=$(vcgencmd get_throttled 2>/dev/null || echo "")
    if [[ "$raw_throttled" =~ throttled=(0x[0-9a-fA-F]+) ]]; then
        tval=$(( BASH_REMATCH[1] ))
        if (( (tval & 0x1) != 0 )); then
            power_str="UNDERVOLT"
        elif (( (tval & 0x10000) != 0 )); then
            power_str="undervolt-occurred"
        fi
    fi
fi

msg="Daily digest | Up: ${uptime_str} | Temp: ${temp_str} (${power_str}) | WAN: ${utype} | TS: ${ts_state} | AP clients: ${clients} | Data: ${data_today} | Failed: ${failed}"

/usr/local/bin/notify-router.sh "$msg" low || logger -t daily-digest "$msg"
