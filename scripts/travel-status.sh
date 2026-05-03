#!/bin/bash
# One-shot travel router status — safe to run without sudo.
# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

C='\033[0;36m'; G='\033[0;32m'; NC='\033[0m'; W='\033[1;37m'; DIM='\033[2m'
_flag() { [[ "${!1:-0}" = "1" ]] && printf "${G}on${NC}" || printf "${DIM}off${NC}"; }

printf "${C}━━ Travel Router ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

# Active uplink (derive from default route)
uplink=$(ip route get 1.1.1.1 2>/dev/null | awk '/dev/{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1);exit}}}')
src_ip=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++){if($i=="src"){print $(i+1);exit}}}')
case "$uplink" in
    enx*) utype="iPhone USB" ;; rndis0|usb0) utype="Android USB" ;;
    bnep0) utype="BT PAN" ;; wlan0) utype="WiFi STA" ;;
    tailscale0) utype="Tailscale" ;; *) utype="${uplink:-none}" ;;
esac
printf "  ${W}Uplink${NC}:   ${G}%s${NC} (%s)  src: %s\n" "${uplink:-none}" "$utype" "${src_ip:-?}"

# Tailscale
ts_status=$(tailscale status --self 2>/dev/null | head -1 || printf "unavailable")
ts_ip=$(tailscale ip -4 2>/dev/null | head -1 || true)
printf "  ${W}Tailscale${NC}: %s  %s\n" "$ts_status" "${ts_ip:-}"

# AP clients
ap_ssid=$(grep "^ssid=" /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2 || printf "unknown")
ap_clients=$(iw dev uap0 station dump 2>/dev/null | grep -c "^Station" || printf "0")
printf "  ${W}AP${NC}:       %s  clients: %s\n" "$ap_ssid" "$ap_clients"

printf "  ${W}Features${NC}: DoT=$(_flag ENABLE_DOT) Blocklist=$(_flag ENABLE_BLOCKLISTS) KillSwitch=$(_flag ENABLE_VPN_KILLSWITCH) AdGuard=$(_flag ENABLE_ADGUARD) Avahi=$(_flag ENABLE_AVAHI_REFLECTOR)\n"

printf "${C}━━ System ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

temp=$(awk '{printf "%.0f°C", $1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null || printf "?")
uptime_str=$(uptime -p 2>/dev/null | sed 's/up //' || printf "?")
cpu=$(top -bn1 2>/dev/null | awk '/^%Cpu/{printf "%.0f%%", 100-$8}' || printf "?")
printf "  CPU: %s  Temp: %s  Up: %s\n" "$cpu" "$temp" "$uptime_str"
free -m 2>/dev/null | awk '/^Mem/{printf "  RAM: %dM used / %dM total\n", $3, $2}'
df -h / 2>/dev/null | awk 'NR==2{printf "  Disk: %s/%s (%s used)\n", $3, $2, $5}'
printf "  Version: %s\n" "$(cat /etc/travel-router-version 2>/dev/null || printf "unknown")"
printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
