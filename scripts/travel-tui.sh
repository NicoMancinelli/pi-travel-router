#!/bin/bash
# Interactive TUI dashboard for the Pi Travel Router.
# Usage: sudo travel-tui

set -euo pipefail
[[ $EUID -ne 0 ]] && { printf "Run as root: sudo travel-tui\n" >&2; exit 1; }

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

R='\033[0;31m'; G='\033[0;32m'; C='\033[0;36m'
NC='\033[0m'; W='\033[1;37m'; DIM='\033[2m'; BOLD='\033[1m'

_cleanup() { clear; exit 0; }
trap _cleanup INT TERM

_flag_color() { [[ "${!1:-0}" = "1" ]] && printf "${G}on${NC}" || printf "${DIM}off${NC}"; }

_fmt_bps() {
    awk -v b="$1" 'BEGIN{
        if(b>=1048576) printf "%.1f MB/s", b/1048576
        else if(b>=1024) printf "%d KB/s", b/1024
        else printf "%d B/s", b
    }'
}

_bw_delta() {
    local iface=$1 dir=$2
    local prev_file="/tmp/tui_${dir}_${iface}"
    local cur prev delta
    cur=$(cat "/sys/class/net/${iface}/statistics/${dir}_bytes" 2>/dev/null || echo 0)
    cur=$(( cur + 0 ))
    prev=$(cat "$prev_file" 2>/dev/null || echo "$cur")
    prev=$(( prev + 0 ))
    echo "$cur" > "$prev_file"
    delta=$(( cur > prev ? (cur - prev) / 5 : 0 ))
    echo "$delta"
}

draw_dashboard() {
    clear
    # shellcheck source=/dev/null
    source /etc/default/travel-router 2>/dev/null || true

    local version uplink utype src_ip ts_ip ap_ssid ap_clients
    local temp uptime_str cpu signal bw_up bw_dn ts_peers ts_label client_ips

    version=$(cat /etc/travel-router-version 2>/dev/null || printf "unknown")
    printf "${BOLD}${C}Pi Travel Router${NC}  v%s  %s\n" "$version" "$(date '+%Y-%m-%d %H:%M:%S')"
    printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

    uplink=$(ip route get 1.1.1.1 2>/dev/null \
        | awk '/dev/{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1);exit}}}')
    src_ip=$(ip route get 1.1.1.1 2>/dev/null \
        | awk '/src/{for(i=1;i<=NF;i++){if($i=="src"){print $(i+1);exit}}}')
    case "$uplink" in
        enx*) utype="iPhone USB" ;; rndis0|usb0) utype="Android USB" ;;
        bnep0) utype="BT PAN" ;; wlan0) utype="WiFi STA" ;;
        tailscale0) utype="Tailscale" ;; *) utype="${uplink:-none}" ;;
    esac

    bw_up=$(_bw_delta "${uplink:-lo}" tx)
    bw_dn=$(_bw_delta "${uplink:-lo}" rx)
    printf "  ${W}Uplink${NC}:   ${G}%s${NC} (%s)  src: %s\n" \
        "${uplink:-none}" "$utype" "${src_ip:-?}"
    printf "           up %s  dn %s\n" "$(_fmt_bps "$bw_up")" "$(_fmt_bps "$bw_dn")"

    signal=$(iw dev wlan0 link 2>/dev/null | awk '/signal/{print $2, $3}' || true)
    [[ -n "$signal" ]] && printf "  ${W}WiFi STA${NC}: signal %s\n" "$signal"

    ts_label="Tailscale"
    [[ -n "${HEADSCALE_URL:-}" ]] && ts_label="Headscale"
    ts_ip=$(tailscale ip -4 2>/dev/null | head -1 || true)
    ts_peers=$(tailscale status --json 2>/dev/null \
        | awk -F'"Online":' 'NF>1 && $2~/^[[:space:]]*true/{c++} END{print c+0}' || echo "?")
    printf "  ${W}%-9s${NC}: %-15s  peers online: %s\n" \
        "$ts_label" "${ts_ip:-not connected}" "$ts_peers"

    ap_ssid=$(grep "^ssid=" /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2 \
        || printf "unknown")
    ap_clients=$(iw dev uap0 station dump 2>/dev/null | grep -c "^Station" || printf "0")
    client_ips=$(ip neigh show dev uap0 2>/dev/null | awk '{printf "%s ", $1}' | head -c 48 || true)
    printf "  ${W}AP${NC}:       %s  clients: %s%s\n" \
        "$ap_ssid" "$ap_clients" \
        "${client_ips:+  (${client_ips%% })}"

    printf "  ${W}Features${NC}: DoT=$(_flag_color ENABLE_DOT) Blocklist=$(_flag_color ENABLE_BLOCKLISTS) KillSwitch=$(_flag_color ENABLE_VPN_KILLSWITCH) AdGuard=$(_flag_color ENABLE_ADGUARD) Avahi=$(_flag_color ENABLE_AVAHI_REFLECTOR)\n"

    printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
    temp=$(awk '{printf "%.0fC", $1/1000}' /sys/class/thermal/thermal_zone0/temp \
        2>/dev/null || printf "?")
    uptime_str=$(uptime -p 2>/dev/null | sed 's/up //' || printf "?")
    cpu=$(top -bn1 2>/dev/null | awk '/^%Cpu/{printf "%.0f%%", 100-$8}' || printf "?")
    printf "  CPU: %s  Temp: %s  Up: %s\n" "$cpu" "$temp" "$uptime_str"
    free -m 2>/dev/null | awk '/^Mem/{printf "  RAM: %dM used / %dM total\n", $3, $2}'
    df -h / 2>/dev/null | awk 'NR==2{printf "  Disk: %s/%s (%s used)\n", $3, $2, $5}'

    printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
    printf "  [1] Services  [2] Features  [3] Logs  [4] Clients  [5] Network  [6] System  [q] Quit\n"
    printf "  (auto-refresh every 5s)\n"
}

show_services() {
    clear
    local -a svc_list
    svc_list=(
        tailscaled hostapd dnsmasq stubby adguard-home
        tor privoxy failover-watchdog wan-watchdog tailscale-watchdog
    )

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
        printf "  [%2d] %-28s %b\n" "$i" "$svc" "$status_str"
        (( i++ )) || true
    done

    printf "\n  Enter number to restart, [q] to return: "
    local choice
    read -r choice
    case "$choice" in
        q|Q) return ;;
        [1-9]|10)
            local idx
            idx=$(( choice - 1 ))
            if [[ $idx -lt ${#svc_list[@]} ]]; then
                local target="${svc_list[$idx]}"
                printf "  Restarting %s...\n" "$target"
                if systemctl restart "$target" 2>/dev/null; then
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
        ENABLE_CLIENT_QOS
        ENABLE_PER_DEVICE_VPN
        ENABLE_CAKE_AUTOTUNE
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
        printf "  [%2d] %-38s %b\n" "$i" "$flag" "$status_str"
        (( i++ )) || true
    done

    printf "\n  Enter number to toggle, [q] to return: "
    local choice
    read -r choice
    case "$choice" in
        q|Q) return ;;
        [1-9]|1[0-3])
            local idx
            idx=$(( choice - 1 ))
            if [[ $idx -lt ${#flag_list[@]} ]]; then
                local target_flag="${flag_list[$idx]}"
                local cur_val new_val
                cur_val="${!target_flag:-0}"
                if [[ "$cur_val" = "1" ]]; then
                    new_val="0"
                else
                    new_val="1"
                fi
                sed -i "s/^${target_flag}=.*/${target_flag}=\"${new_val}\"/" \
                    /etc/default/travel-router
                printf "  ${G}ok${NC} — %s -> %s\n" "$target_flag" "$new_val"

                # Apply the change immediately
                case "$target_flag" in
                    ENABLE_VPN_KILLSWITCH|ENABLE_TOR_TRANSPARENT|ENABLE_BLOCKLISTS|ENABLE_PER_DEVICE_VPN)
                        printf "  Reloading firewall...\n"
                        /usr/local/bin/travel-router-firewall.sh --save 2>/dev/null \
                            && printf "  ${G}ok${NC} — firewall reloaded\n" \
                            || printf "  ${R}warn${NC} — firewall reload failed\n"
                        if [[ "$target_flag" = "ENABLE_BLOCKLISTS" && "$new_val" = "1" ]]; then
                            printf "  Triggering blocklist update...\n"
                            systemctl start update-blocklists.service 2>/dev/null || true
                        fi
                        ;;
                    ENABLE_DOT)
                        if [[ "$new_val" = "1" ]]; then
                            systemctl restart stubby 2>/dev/null || true
                        else
                            systemctl stop stubby 2>/dev/null || true
                        fi
                        systemctl reload-or-restart dnsmasq 2>/dev/null || true
                        printf "  ${G}ok${NC} — DoT %s\n" "$([[ $new_val = 1 ]] && echo enabled || echo disabled)"
                        ;;
                    ENABLE_ADGUARD)
                        if [[ "$new_val" = "1" ]]; then
                            systemctl restart adguard-home 2>/dev/null || true
                        else
                            systemctl stop adguard-home 2>/dev/null || true
                        fi
                        systemctl reload-or-restart dnsmasq 2>/dev/null || true
                        printf "  ${G}ok${NC} — AdGuard Home %s\n" "$([[ $new_val = 1 ]] && echo enabled || echo disabled)"
                        ;;
                    ENABLE_AVAHI_REFLECTOR)
                        systemctl reload-or-restart avahi-daemon 2>/dev/null || true
                        printf "  ${G}ok${NC} — avahi-daemon restarted\n"
                        ;;
                    ENABLE_HTTP_UA_REWRITE)
                        if [[ "$new_val" = "1" ]]; then
                            systemctl restart privoxy 2>/dev/null || true
                        else
                            systemctl stop privoxy 2>/dev/null || true
                        fi
                        printf "  ${G}ok${NC} — privoxy %s\n" "$([[ $new_val = 1 ]] && echo started || echo stopped)"
                        ;;
                    ENABLE_AP_SCHEDULE)
                        if [[ "$new_val" = "1" ]]; then
                            systemctl enable --now ap-disable.timer ap-enable.timer 2>/dev/null || true
                            printf "  ${G}ok${NC} — AP schedule timers enabled\n"
                        else
                            systemctl disable --now ap-disable.timer ap-enable.timer 2>/dev/null || true
                            printf "  ${G}ok${NC} — AP schedule timers disabled\n"
                        fi
                        ;;
                    ENABLE_CAKE_AUTOTUNE)
                        if [[ "$new_val" = "1" ]]; then
                            systemctl enable --now tune-cake.timer 2>/dev/null || true
                            printf "  ${G}ok${NC} — CAKE autotune timer enabled\n"
                        else
                            systemctl disable --now tune-cake.timer 2>/dev/null || true
                            printf "  ${G}ok${NC} — CAKE autotune timer disabled\n"
                        fi
                        ;;
                    ENABLE_CLIENT_QOS)
                        /usr/local/bin/apply-cake.sh 2>/dev/null || true
                        printf "  ${G}ok${NC} — CAKE qdisc re-applied\n"
                        ;;
                    ENABLE_AUTO_UPDATES)
                        if [[ "$new_val" = "1" ]]; then
                            systemctl enable unattended-upgrades 2>/dev/null || true
                        else
                            systemctl disable unattended-upgrades 2>/dev/null || true
                        fi
                        printf "  ${G}ok${NC} — unattended-upgrades %s\n" "$([[ $new_val = 1 ]] && echo enabled || echo disabled)"
                        ;;
                    *)
                        printf "  ${DIM}note${NC} — flag written; restart affected service manually if needed\n"
                        ;;
                esac
                sleep 2
            fi
            ;;
    esac
}

show_logs() {
    while true; do
        clear
        printf "${BOLD}${C}Logs${NC}\n"
        printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
        printf "  [1] wan-watchdog.log (last 25 lines)\n"
        printf "  [2] tailscaled journal (last 25 lines)\n"
        printf "  [3] failover-watchdog journal (last 25 lines)\n"
        printf "  [4] Failed systemd units\n"
        printf "\n  Enter choice, [q] to return: "
        local choice
        read -r choice
        case "$choice" in
            1)
                clear; printf "${W}wan-watchdog.log:${NC}\n"
                tail -n 25 /var/log/wan-watchdog.log 2>/dev/null || printf "  (no log found)\n"
                printf "\n  Press any key..."; read -rsn1 || true ;;
            2)
                clear; printf "${W}tailscaled (last 25):${NC}\n"
                journalctl -u tailscaled -n 25 --no-pager 2>/dev/null || printf "  (unavailable)\n"
                printf "\n  Press any key..."; read -rsn1 || true ;;
            3)
                clear; printf "${W}failover-watchdog (last 25):${NC}\n"
                journalctl -u failover-watchdog -n 25 --no-pager 2>/dev/null \
                    || printf "  (unavailable)\n"
                printf "\n  Press any key..."; read -rsn1 || true ;;
            4)
                clear; printf "${W}Failed units:${NC}\n"
                systemctl --failed --no-pager 2>/dev/null || true
                printf "\n  Press any key..."; read -rsn1 || true ;;
            q|Q) return ;;
        esac
    done
}

show_clients() {
    clear
    printf "${BOLD}${C}AP Clients${NC}\n"
    printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

    local station_dump client_count
    station_dump=$(iw dev uap0 station dump 2>/dev/null || true)
    client_count=$(printf '%s' "$station_dump" | grep -c "^Station" || true)

    if [[ "${client_count:-0}" -eq 0 ]]; then
        printf "  No clients connected.\n"
        printf "\n  Press any key..."; read -rsn1 || true
        return
    fi

    printf "  %-18s  %-16s  %s\n" "MAC" "IP" "Signal"
    printf "  %-18s  %-16s  %s\n" "------------------" "----------------" "-------"

    local _mac _ip _signal
    _mac=""; _ip=""; _signal="?"
    while IFS= read -r _line; do
        case "$_line" in
            "Station "*)
                _mac=$(printf '%s' "$_line" | awk '{print $2}')
                _ip=$(ip neigh show dev uap0 2>/dev/null \
                    | awk -v m="$_mac" 'tolower($3)==tolower(m){print $1; exit}')
                [[ -z "$_ip" ]] && _ip="unknown"
                _signal="?"
                ;;
            *"signal:"*)
                _signal=$(printf '%s' "$_line" | awk '{print $2, $3}')
                printf "  %-18s  %-16s  %s\n" "$_mac" "$_ip" "$_signal"
                ;;
        esac
    done <<< "$station_dump"

    printf "\n  [r] Refresh  [q] Return: "
    local _choice
    read -r _choice
    case "$_choice" in
        r|R) show_clients ;;
        *) return ;;
    esac
}

show_network() {
    while true; do
        clear
        printf "${BOLD}${C}Network Tools${NC}\n"
        printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
        printf "  [1] Show WiFi QR code\n"
        printf "  [2] Run speedtest + update CAKE bandwidth (~30s)\n"
        printf "  [3] Clone MAC to wlan0 (captive portal bypass)\n"
        printf "  [4] Restore original wlan0 MAC\n"
        printf "  [5] Real-time bandwidth (bmon)\n"
        printf "  [6] Per-connection traffic (iftop on uap0)\n"
        printf "  [7] Connect to hotel/new WiFi network\n"
        printf "\n  Enter choice, [q] to return: "
        local choice
        read -r choice
        case "$choice" in
            1)
                clear; printf "${W}WiFi QR Code:${NC}\n"
                cat /usr/local/share/travel-router/wifi-qr/wifi-qr.txt 2>/dev/null \
                    || printf "  QR not generated (qrencode not installed at setup time)\n"
                printf "\n  Press any key..."; read -rsn1 || true ;;
            2)
                clear; printf "${W}Running speedtest...${NC}\n"
                /usr/local/bin/tune-cake.sh 2>&1 || \
                    printf "  ${R}failed${NC} — check: journalctl -u tune-cake\n"
                printf "\n  Press any key..."; read -rsn1 || true ;;
            3)
                printf "\n  Enter MAC to clone (e.g. aa:bb:cc:dd:ee:ff): "
                local mac_in
                read -r mac_in
                if [[ "$mac_in" =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]]; then
                    /usr/local/bin/clone-mac.sh "$mac_in" 2>&1 && \
                        printf "  ${G}ok${NC} — MAC cloned to wlan0\n" || \
                        printf "  ${R}failed${NC}\n"
                else
                    printf "  ${R}Invalid MAC format${NC}\n"
                fi
                sleep 2 ;;
            4)
                /usr/local/bin/clone-mac.sh --restore 2>&1 && \
                    printf "  ${G}ok${NC} — original MAC restored\n" || \
                    printf "  ${R}failed${NC}\n"
                sleep 2 ;;
            5)
                if command -v bmon >/dev/null 2>&1; then
                    bmon
                else
                    printf "  bmon not installed\n  Press any key..."; read -rsn1 || true
                fi ;;
            6)
                if command -v iftop >/dev/null 2>&1; then
                    iftop -i uap0 2>/dev/null || true
                else
                    printf "  iftop not installed\n  Press any key..."; read -rsn1 || true
                fi ;;
            7)
                clear; printf "${W}Connect to WiFi${NC}\n"
                printf "  Scanning...\n"
                nmcli --fields SSID,SIGNAL,SECURITY device wifi list 2>/dev/null | head -20 || true
                printf "\n  SSID to connect to: "
                local wifi_ssid
                read -r wifi_ssid
                [[ -z "$wifi_ssid" ]] && break
                printf "  Password (blank for open network): "
                local wifi_pass
                read -rs wifi_pass; printf "\n"
                if [[ -n "$wifi_pass" ]]; then
                    nmcli device wifi connect "$wifi_ssid" password "$wifi_pass" ifname wlan0 2>&1 \
                        && printf "  ${G}ok${NC} — connected to %s\n" "$wifi_ssid" \
                        || printf "  ${R}failed${NC} — check SSID/password and try again\n"
                else
                    nmcli device wifi connect "$wifi_ssid" ifname wlan0 2>&1 \
                        && printf "  ${G}ok${NC} — connected to %s\n" "$wifi_ssid" \
                        || printf "  ${R}failed${NC} — could not connect\n"
                fi
                sleep 3 ;;
            q|Q) return ;;
        esac
    done
}

show_system() {
    clear
    printf "${BOLD}${C}System Actions${NC}\n"
    printf "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
    printf "  [1] Reboot now\n"
    printf "  [2] Shutdown now\n"
    printf "  [3] Run update-router.sh\n"
    printf "  [4] Send daily digest now\n"
    printf "  [5] Reload firewall\n"
    printf "\n  Enter choice, [q] to return: "
    local choice
    read -r choice
    case "$choice" in
        q|Q) return ;;
        1) printf "  Rebooting...\n"; reboot ;;
        2) printf "  Shutting down...\n"; shutdown -h now ;;
        3)
            clear; printf "${W}Running update-router.sh...${NC}\n"
            /usr/local/bin/update-router.sh 2>&1 || true
            printf "\n  Press any key..."; read -rsn1 || true ;;
        4)
            /usr/local/bin/daily-digest.sh 2>&1 || true
            printf "  ${G}ok${NC} — digest sent\n"; sleep 2 ;;
        5)
            printf "  Reloading firewall...\n"
            /usr/local/bin/travel-router-firewall.sh --save 2>/dev/null || true
            printf "  ${G}ok${NC} — firewall reloaded\n"; sleep 2 ;;
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
        4) show_clients ;;
        5) show_network ;;
        6) show_system ;;
        q|Q) _cleanup ;;
    esac
done
