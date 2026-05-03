#!/bin/bash
# Interactive TUI dashboard for the Pi Travel Router.
# Usage: sudo travel-tui

set -euo pipefail
[[ $EUID -ne 0 ]] && { printf "Run as root: sudo travel-tui\n" >&2; exit 1; }

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

R='\033[0;31m'; G='\033[0;32m'; C='\033[0;36m'
NC='\033[0m'; W='\033[1;37m'; DIM='\033[2m'; BOLD='\033[1m'

_flag_color() { [[ "${!1:-0}" = "1" ]] && printf "${G}on${NC}" || printf "${DIM}off${NC}"; }

draw_dashboard() {
    clear
    local version uplink src_ip utype ts_status ts_ip ap_ssid ap_clients
    local temp uptime_str cpu

    version=$(cat /etc/travel-router-version 2>/dev/null || printf "unknown")
    printf "${BOLD}${C}Pi Travel Router${NC}  v%s  %s\n" "$version" "$(date '+%Y-%m-%d %H:%M:%S')"
    printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

    uplink=$(ip route get 1.1.1.1 2>/dev/null | awk '/dev/{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1);exit}}}')
    src_ip=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++){if($i=="src"){print $(i+1);exit}}}')
    case "$uplink" in
        enx*) utype="iPhone USB" ;; rndis0|usb0) utype="Android USB" ;;
        bnep0) utype="BT PAN" ;; wlan0) utype="WiFi STA" ;;
        tailscale0) utype="Tailscale" ;; *) utype="${uplink:-none}" ;;
    esac
    printf "  ${W}Uplink${NC}:   ${G}%s${NC} (%s)  src: %s\n" "${uplink:-none}" "$utype" "${src_ip:-?}"

    ts_status=$(tailscale status --self 2>/dev/null | head -1 || printf "unavailable")
    ts_ip=$(tailscale ip -4 2>/dev/null | head -1 || true)
    printf "  ${W}Tailscale${NC}: %s  %s\n" "$ts_status" "${ts_ip:-}"

    ap_ssid=$(grep "^ssid=" /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2 || printf "unknown")
    ap_clients=$(iw dev uap0 station dump 2>/dev/null | grep -c "^Station" || printf "0")
    printf "  ${W}AP${NC}:       %s  clients: %s\n" "$ap_ssid" "$ap_clients"

    printf "  ${W}Features${NC}: DoT=$(_flag_color ENABLE_DOT) Blocklist=$(_flag_color ENABLE_BLOCKLISTS) KillSwitch=$(_flag_color ENABLE_VPN_KILLSWITCH) AdGuard=$(_flag_color ENABLE_ADGUARD) Avahi=$(_flag_color ENABLE_AVAHI_REFLECTOR)\n"

    printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

    temp=$(awk '{printf "%.0f°C", $1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null || printf "?")
    uptime_str=$(uptime -p 2>/dev/null | sed 's/up //' || printf "?")
    cpu=$(top -bn1 2>/dev/null | awk '/^%Cpu/{printf "%.0f%%", 100-$8}' || printf "?")
    printf "  CPU: %s  Temp: %s  Up: %s\n" "$cpu" "$temp" "$uptime_str"
    free -m 2>/dev/null | awk '/^Mem/{printf "  RAM: %dM used / %dM total\n", $3, $2}'
    df -h / 2>/dev/null | awk 'NR==2{printf "  Disk: %s/%s (%s used)\n", $3, $2, $5}'

    printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
    printf "  [1] Services  [2] Features  [3] Logs  [4] System  [q] Quit  (refreshes every 5s)\n"
}

show_services() {
    clear
    local -a svc_list
    svc_list=(tailscaled hostapd dnsmasq stubby adguard-home tor privoxy failover-watchdog wan-watchdog)

    printf "${BOLD}${C}Services${NC}\n"
    printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

    local i=1
    local svc status_str
    for svc in "${svc_list[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            status_str="${G}active${NC}"
        else
            status_str="${DIM}inactive${NC}"
        fi
        printf "  [%d] %-28s %b\n" "$i" "$svc" "$status_str"
        (( i++ )) || true
    done

    printf "\n  Enter number to restart service, [q] to return: "
    local choice
    read -r choice
    case "$choice" in
        q|Q) return ;;
        [1-9])
            local idx=$(( choice - 1 ))
            if [[ $idx -lt ${#svc_list[@]} ]]; then
                local target="${svc_list[$idx]}"
                printf "  Restarting %s...\n" "$target"
                if systemctl restart "$target" 2>/dev/null || true; then
                    printf "  ${G}ok${NC} — %s restarted\n" "$target"
                else
                    printf "  ${R}fail${NC} — could not restart %s\n" "$target"
                fi
                sleep 2
            fi
            ;;
    esac
}

show_features() {
    clear
    # shellcheck source=/dev/null
    source /etc/default/travel-router 2>/dev/null || true

    local -a flag_list
    flag_list=(
        ENABLE_DOT
        ENABLE_VPN_KILLSWITCH
        ENABLE_AUTO_UPDATES
        ENABLE_AVAHI_REFLECTOR
        ENABLE_ADGUARD
        ENABLE_BLOCKLISTS
        ENABLE_TOR_TRANSPARENT
        ENABLE_HTTP_UA_REWRITE
        ENABLE_OPEN_WIFI_FALLBACK
        ENABLE_AP_SCHEDULE
    )

    printf "${BOLD}${C}Feature Flags${NC}\n"
    printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

    local i=1
    local flag val status_str
    for flag in "${flag_list[@]}"; do
        val="${!flag:-0}"
        if [[ "$val" = "1" ]]; then
            status_str="${G}on${NC}"
        else
            status_str="${DIM}off${NC}"
        fi
        printf "  [%d] %-36s %b\n" "$i" "$flag" "$status_str"
        (( i++ )) || true
    done

    printf "\n  Enter number to toggle, [q] to return: "
    local choice
    read -r choice
    case "$choice" in
        q|Q) return ;;
        [1-9]|10)
            local idx=$(( choice - 1 ))
            if [[ $idx -lt ${#flag_list[@]} ]]; then
                local target_flag="${flag_list[$idx]}"
                local cur_val="${!target_flag:-0}"
                local new_val
                if [[ "$cur_val" = "1" ]]; then
                    new_val="0"
                else
                    new_val="1"
                fi
                sed -i "s/^${target_flag}=.*/${target_flag}=\"${new_val}\"/" /etc/default/travel-router
                printf "  ${G}ok${NC} — %s set to %s\n" "$target_flag" "$new_val"
                if [[ "$target_flag" = "ENABLE_VPN_KILLSWITCH" ]]; then
                    printf "  Reloading firewall...\n"
                    /usr/local/bin/travel-router-firewall.sh --save 2>/dev/null || true
                    printf "  ${G}ok${NC} — firewall reloaded\n"
                fi
                sleep 2
            fi
            ;;
    esac
}

show_logs() {
    clear
    printf "${BOLD}${C}Logs${NC}\n"
    printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
    printf "${W}wan-watchdog.log (last 15 lines):${NC}\n"
    tail -n 15 /var/log/wan-watchdog.log 2>/dev/null || printf "  (no log found)\n"
    printf "\n${W}tailscaled (last 5 lines):${NC}\n"
    journalctl -u tailscaled -n 5 --no-pager 2>/dev/null || printf "  (unavailable)\n"
    printf "\n  Press any key to return..."
    read -rsn1 || true
}

show_system() {
    clear
    printf "${BOLD}${C}System Actions${NC}\n"
    printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
    printf "  [1] Reboot now\n"
    printf "  [2] Shutdown now\n"
    printf "  [3] Run update-router.sh\n"
    printf "  [4] Reload firewall\n"
    printf "\n  Enter choice, [q] to return: "
    local choice
    read -r choice
    case "$choice" in
        q|Q) return ;;
        1)
            printf "  Rebooting...\n"
            reboot
            ;;
        2)
            printf "  Shutting down...\n"
            shutdown -h now
            ;;
        3)
            printf "  Running update-router.sh...\n"
            /usr/local/bin/update-router.sh 2>&1 || true
            printf "\n  Press any key to return..."
            read -rsn1 || true
            ;;
        4)
            printf "  Reloading firewall...\n"
            /usr/local/bin/travel-router-firewall.sh --save 2>/dev/null || true
            printf "  ${G}ok${NC} — firewall reloaded\n"
            sleep 2
            ;;
    esac
}

while true; do
    # shellcheck source=/dev/null
    source /etc/default/travel-router 2>/dev/null || true
    draw_dashboard
    key=""
    read -rsn1 -t 5 key || true
    case "$key" in
        1) show_services ;;
        2) show_features ;;
        3) show_logs ;;
        4) show_system ;;
        q|Q) clear; break ;;
    esac
done
