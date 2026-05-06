#!/bin/bash
# Interactive TUI dashboard for the Pi Travel Router.
# Usage: sudo travel-tui

set -euo pipefail
[[ $EUID -ne 0 ]] && { printf "Run as root: sudo travel-tui\n" >&2; exit 1; }

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

# ── Palette ──────────────────────────────────────────────────────────────────
R='\033[0;31m'; G='\033[0;32m'; C='\033[0;36m'
NC='\033[0m'; W='\033[1;37m'; DIM='\033[2m'; BOLD='\033[1m'

_cleanup() { tput cnorm 2>/dev/null || true; clear; exit 0; }
trap _cleanup INT TERM

# ── Box drawing ───────────────────────────────────────────────────────────────
# Outer width: 68 chars  (╔ + 66═ + ╗)
# Content area: 62 chars  (║ + 2sp + 62 + 2sp + ║ = 68)
_HR='══════════════════════════════════════════════════════════════════'
_TOP="╔${_HR}╗"; _SEP="╠${_HR}╣"; _BOT="╚${_HR}╝"

_box_top() { printf "${C}${_TOP}${NC}\n"; }
_box_sep() { printf "${C}${_SEP}${NC}\n"; }
_box_bot() { printf "${C}${_BOT}${NC}\n"; }

# Plain text line — printf %-62s handles padding correctly (no ANSI inside $1)
_bl() { printf "${C}║${NC}  %-62s  ${C}║${NC}\n" "$1"; }

# Empty line inside box
_be() { _bl ""; }

# Colored/ANSI line — strip ANSI to measure visible width, then pad manually
_cl() {
    local content="$1" vis pad
    vis=$(printf '%s' "$content" | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g; s/\x1b(B//g')
    pad=$(( 62 - ${#vis} ))
    [[ $pad -lt 0 ]] && pad=0
    printf "${C}║${NC}  %s%${pad}s  ${C}║${NC}\n" "$content" ""
}

# Right-aligned two-column line (left + right, auto padded to 62 visible chars)
_rl() {
    local left="$1" right="$2"
    local lv rv pad
    lv=$(printf '%s' "$left"  | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g; s/\x1b(B//g')
    rv=$(printf '%s' "$right" | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g; s/\x1b(B//g')
    pad=$(( 62 - ${#lv} - ${#rv} ))
    [[ $pad -lt 1 ]] && pad=1
    printf "${C}║${NC}  %s%${pad}s%s  ${C}║${NC}\n" "$left" "" "$right"
}

# ── Indicator helpers ────────────────────────────────────────────────────────
_dot()  { [[ "${!1:-0}" = "1" ]] && printf "${G}●${NC}" || printf "${DIM}○${NC}"; }
_onoff(){ [[ "${!1:-0}" = "1" ]] && printf "${G}on${NC}" || printf "${DIM}off${NC}"; }
_svc_dot() {
    systemctl is-active --quiet "$1" 2>/dev/null \
        && printf "${G}●${NC}" || printf "${R}○${NC}"
}

_fmt_bps() {
    awk -v b="$1" 'BEGIN{
        if(b>=1048576) printf "%.1f MB/s", b/1048576
        else if(b>=1024) printf "%d KB/s", b/1024
        else printf "%d B/s", b
    }'
}

_bw_delta() {
    local iface="$1" dir="$2" cur prev
    local prev_file="/tmp/tui_${dir}_${iface}"
    cur=$(cat "/sys/class/net/${iface}/statistics/${dir}_bytes" 2>/dev/null || echo 0)
    cur=$(( cur + 0 ))
    prev=$(cat "$prev_file" 2>/dev/null || echo "$cur")
    prev=$(( prev + 0 ))
    echo "$cur" > "$prev_file"
    echo $(( cur > prev ? (cur - prev) / 5 : 0 ))
}

# ── Config editing helpers ────────────────────────────────────────────────────
# Edit a variable in /etc/default/travel-router
_cfg_edit() {
    local varname="$1" label="$2" secret="${3:-0}"
    # shellcheck source=/dev/null
    source /etc/default/travel-router 2>/dev/null || true
    local cur="${!varname:-}" new_val

    printf "\n  ${W}%s${NC}\n" "$label"
    if [[ "$secret" = "1" && -n "$cur" ]]; then
        printf "  Current: ${DIM}(set — %d chars)${NC}\n" "${#cur}"
    else
        printf "  Current: ${DIM}%s${NC}\n" "${cur:-(empty)}"
    fi
    printf "  New value (Enter to keep): "
    if [[ "$secret" = "1" ]]; then
        read -rs new_val; printf "\n"
    else
        read -r new_val
    fi
    [[ -z "$new_val" ]] && { printf "  ${DIM}(unchanged)${NC}\n"; return; }
    if grep -q "^${varname}=" /etc/default/travel-router 2>/dev/null; then
        sed -i "s|^${varname}=.*|${varname}=\"${new_val}\"|" /etc/default/travel-router
    else
        printf '\n%s="%s"\n' "$varname" "$new_val" >> /etc/default/travel-router
    fi
    printf "  ${G}✓ Saved${NC}\n"
}

_ap_edit_ssid() {
    local cur new_val
    cur=$(grep "^ssid=" /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2 || echo "")
    printf "\n  ${W}AP Network Name (SSID)${NC}\n  Current: ${DIM}%s${NC}\n  New value (Enter to keep): " "${cur:-(unknown)}"
    read -r new_val
    [[ -z "$new_val" ]] && { printf "  ${DIM}(unchanged)${NC}\n"; return; }
    sed -i "s/^ssid=.*/ssid=${new_val}/" /etc/hostapd/hostapd.conf
    printf "  ${G}✓ Saved${NC} — restarting hostapd...\n"
    systemctl restart hostapd 2>/dev/null \
        && printf "  ${G}✓ hostapd restarted — AP is now %s${NC}\n" "$new_val" \
        || printf "  ${R}✗ hostapd restart failed${NC}\n"
}

_ap_edit_pass() {
    local cur new_val
    cur=$(grep "^wpa_passphrase=" /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2 || echo "")
    printf "\n  ${W}AP Password${NC}\n"
    [[ -n "$cur" ]] \
        && printf "  Current: ${DIM}(set — %d chars)${NC}\n" "${#cur}" \
        || printf "  Current: ${DIM}(empty)${NC}\n"
    printf "  New password (8–63 chars, Enter to keep): "
    read -rs new_val; printf "\n"
    [[ -z "$new_val" ]] && { printf "  ${DIM}(unchanged)${NC}\n"; return; }
    if [[ ${#new_val} -lt 8 || ${#new_val} -gt 63 ]]; then
        printf "  ${R}✗ Password must be 8–63 characters${NC}\n"
        return
    fi
    sed -i "s/^wpa_passphrase=.*/wpa_passphrase=${new_val}/" /etc/hostapd/hostapd.conf
    printf "  ${G}✓ Saved${NC} — restarting hostapd...\n"
    systemctl restart hostapd 2>/dev/null \
        && printf "  ${G}✓ hostapd restarted${NC}\n" \
        || printf "  ${R}✗ hostapd restart failed${NC}\n"
}

# ── Dashboard ─────────────────────────────────────────────────────────────────
draw_dashboard() {
    clear
    tput civis 2>/dev/null || true
    # shellcheck source=/dev/null
    source /etc/default/travel-router 2>/dev/null || true

    local ver uplink utype src_ip ts_ip ts_peers ts_label ts_dot
    local ap_ssid ap_clients client_ips ap_dot
    local bw_up bw_dn signal temp cpu ram_info disk_info up_str

    ver=$(cat /etc/travel-router-version 2>/dev/null || printf "unknown")

    _box_top
    local date_str lv pad
    date_str=$(date '+%Y-%m-%d  %H:%M:%S')
    lv=$(printf '%s' "▶ Pi Travel Router  v${ver}" | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g')
    pad=$(( 62 - ${#lv} - ${#date_str} ))
    [[ $pad -lt 1 ]] && pad=1
    printf "${C}║${NC}  ${BOLD}${C}%s${NC}%${pad}s${DIM}%s${NC}  ${C}║${NC}\n" \
        "▶ Pi Travel Router  ${DIM}v${ver}" "" "$date_str"
    _box_sep

    # ── Uplink ────────────────────────────────────────────────────────────────
    # Prefer failover state file; fall back to routing table (captive-portal safe)
    local _uplink_state_file="/var/lib/travel-router/uplink.state"
    if [[ -f "$_uplink_state_file" ]]; then
        uplink=$(cat "$_uplink_state_file")
    else
        uplink=$(ip route get 1.1.1.1 2>/dev/null \
            | awk '/dev/{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1);exit}}}' || true)
        if [[ -z "${uplink:-}" ]]; then
            uplink=$(ip route show default 2>/dev/null \
                | awk 'BEGIN{m=99999;iface=""} /^default/{
                    for(i=1;i<=NF;i++){if($i=="dev")d=$(i+1); if($i=="metric")mt=$(i+1)}
                    if(mt=="")mt=0
                    if(mt<m){m=mt;iface=d}}
                  END{print iface}' || true)
        fi
    fi
    src_ip=$(ip route get 1.1.1.1 2>/dev/null \
        | awk '/src/{for(i=1;i<=NF;i++){if($i=="src"){print $(i+1);exit}}}' || true)
    case "${uplink:-}" in
        enx*) utype="iPhone USB"   ;;
        rndis0|usb0) utype="Android USB" ;;
        bnep0) utype="BT PAN"      ;;
        wlan0) utype="WiFi STA"    ;;
        tailscale0) utype="Tailscale" ;;
        *) utype="${uplink:-none}" ;;
    esac

    bw_up=$(_bw_delta "${uplink:-lo}" tx)
    bw_dn=$(_bw_delta "${uplink:-lo}" rx)
    # RSSI only meaningful when uplink is wlan0
    signal=""
    [[ "${uplink:-}" = "wlan0" ]] && \
        signal=$(iw dev wlan0 link 2>/dev/null | awk '/signal/{print $2, $3}' || true)

    local up_dot up_color
    if [[ -n "${uplink:-}" ]]; then
        up_dot="${G}●${NC}"; up_color="$G"
    else
        up_dot="${R}○${NC}"; up_color="$R"
    fi

    # Build uplink label — append RSSI when on WiFi STA
    local utype_disp="$utype"
    [[ -n "$signal" ]] && utype_disp="${utype} · ${signal} dBm"

    # Captive portal inline flag
    local cp_flag=""
    [ -f /tmp/captive-portal-active ] && cp_flag="  ${R}${BOLD}⚠ CAPTIVE PORTAL${NC}"

    _cl "  ${W}UPLINK${NC}    ${up_dot} ${up_color}${utype_disp}${NC}  ${DIM}${uplink:-none}${NC}  ${DIM}src ${src_ip:-?}${NC}${cp_flag}"
    _cl "            ${DIM}↑${NC} $(_fmt_bps "$bw_up")  ${DIM}↓${NC} $(_fmt_bps "$bw_dn")"
    _box_sep

    # ── Tailscale ─────────────────────────────────────────────────────────────
    ts_label="Tailscale"
    [[ -n "${HEADSCALE_URL:-}" ]] && ts_label="Headscale"
    ts_ip=$(tailscale ip -4 2>/dev/null | head -1 || true)
    ts_peers=$(tailscale status --json 2>/dev/null \
        | awk -F'"Online":' 'NF>1 && $2~/^[[:space:]]*true/{c++} END{print c+0}' || echo "?")
    [[ -n "${ts_ip:-}" ]] && ts_dot="${G}●${NC}" || ts_dot="${DIM}○${NC}"
    _cl "  ${W}${ts_label}${NC}  ${ts_dot} ${ts_ip:-not connected}  ${DIM}${ts_peers} peers online${NC}"
    _box_sep

    # ── Access Point ──────────────────────────────────────────────────────────
    ap_ssid=$(grep "^ssid=" /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2 || printf "?")
    ap_clients=$(iw dev uap0 station dump 2>/dev/null | grep -c "^Station" || printf "0")
    # Build comma-separated IP list by cross-referencing station MACs against
    # ip neigh (primary) then /proc/net/arp (fallback).
    client_ips=""
    if [[ "${ap_clients:-0}" -gt 0 ]]; then
        local _neigh_table _arp_table _mac_list _ip_list _ip _m
        _neigh_table=$(ip neigh show 2>/dev/null || true)
        _arp_table=$(awk 'NR>1{print $4, $1}' /proc/net/arp 2>/dev/null || true)
        _mac_list=$(iw dev uap0 station dump 2>/dev/null \
            | awk '/^Station/{print $2}' || true)
        _ip_list=""
        while IFS= read -r _m; do
            [[ -z "$_m" ]] && continue
            _ip=$(printf '%s' "$_neigh_table" \
                | awk -v m="$_m" 'tolower($5)==tolower(m){print $1; exit}')
            if [[ -z "$_ip" ]]; then
                _ip=$(printf '%s' "$_arp_table" \
                    | awk -v m="$_m" 'tolower($1)==tolower(m){print $2; exit}')
            fi
            [[ -n "$_ip" ]] && _ip_list="${_ip_list:+${_ip_list}, }${_ip}"
        done <<< "$_mac_list"
        client_ips="$_ip_list"
    fi
    systemctl is-active --quiet hostapd 2>/dev/null \
        && ap_dot="${G}●${NC}" || ap_dot="${R}○${NC}"
    _cl "  ${W}AP${NC}        ${ap_dot} ${G}${ap_ssid}${NC}  ${DIM}${ap_clients} client$( [[ "${ap_clients:-0}" = "1" ]] && echo '' || echo 's' )${NC}${client_ips:+  ${DIM}(${client_ips})${NC}}"
    _box_sep

    # ── Feature flags ─────────────────────────────────────────────────────────
    _cl "  ${W}FEATURES${NC}  DoT $(_dot ENABLE_DOT)  Kill $(_dot ENABLE_VPN_KILLSWITCH)  Tor $(_dot ENABLE_TOR_TRANSPARENT)  Blocks $(_dot ENABLE_BLOCKLISTS)  AdGuard $(_dot ENABLE_ADGUARD)  2FA $(_dot ENABLE_2FA)  QoS $(_dot ENABLE_CLIENT_QOS)"
    _box_sep

    # ── System stats ──────────────────────────────────────────────────────────
    temp=$(awk '{printf "%.0f°C", $1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null || printf "?")
    up_str=$(uptime -p 2>/dev/null | sed 's/up //' || printf "?")
    cpu=$(top -bn1 2>/dev/null | awk '/^%Cpu/{printf "%.0f%%", 100-$8}' || printf "?")
    ram_info=$(free -m 2>/dev/null | awk '/^Mem/{printf "%dM/%dM", $3, $2}' || printf "?")
    disk_info=$(df -h / 2>/dev/null | awk 'NR==2{printf "%s/%s", $3, $2}' || printf "?")
    _cl "  ${DIM}CPU${NC} ${cpu}  ${DIM}Temp${NC} ${temp}  ${DIM}RAM${NC} ${ram_info}  ${DIM}Disk${NC} ${disk_info}  ${DIM}Up${NC} ${up_str}"
    _box_sep

    # ── Captive portal warning ────────────────────────────────────────────────
    if [ -f /tmp/captive-portal-active ]; then
        _cl "  ${R}${BOLD}⚠  CAPTIVE PORTAL ACTIVE${NC}  ${DIM}Open a browser on your device to log in,${NC}"
        _cl "  ${DIM}then run Network → [h] Re-check portal  (or wait ~60s for auto-check)${NC}"
        _box_sep
    fi

    # ── Navigation ────────────────────────────────────────────────────────────
    _bl "  [1] Services  [2] Features  [3] Logs  [4] Clients  [5] Network"
    _bl "  [6] Settings  [7] System                              [q] Quit"
    _box_bot
    tput cnorm 2>/dev/null || true
}

# ── Services ──────────────────────────────────────────────────────────────────
show_services() {
    local -a svc_list=(
        tailscaled hostapd dnsmasq stubby adguard-home
        tor privoxy failover-watchdog wan-watchdog tailscale-watchdog
    )

    while true; do
        clear
        _box_top
        _cl "  ${BOLD}${C}Services${NC}"
        _box_sep
        local i=1
        for svc in "${svc_list[@]}"; do
            local dot; dot=$(_svc_dot "$svc")
            _cl "  [${i}] ${dot} ${svc}"
            (( i++ )) || true
        done
        _box_sep
        _bl "  Enter number to restart, [q] to return: "
        _box_bot

        local choice
        read -r choice
        case "$choice" in
            q|Q) return ;;
            [1-9]|10)
                local idx=$(( choice - 1 ))
                if [[ $idx -lt ${#svc_list[@]} ]]; then
                    local target="${svc_list[$idx]}"
                    printf "  Restarting %s...\n" "$target"
                    if systemctl restart "$target" 2>/dev/null; then
                        printf "  ${G}✓ %s restarted${NC}\n" "$target"
                    else
                        printf "  ${R}✗ could not restart %s${NC}\n" "$target"
                    fi
                    sleep 2
                fi
                ;;
        esac
    done
}

# ── Features ──────────────────────────────────────────────────────────────────
show_features() {
    local -a flag_list=(
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
        ENABLE_UPS_MONITOR
        ENABLE_BANDWIDTH_DASHBOARD
        ENABLE_SPLIT_TUNNEL
        ENABLE_2FA
        ENABLE_WAN_METRICS
        ENABLE_PROMETHEUS_EXPORTER
    )

    while true; do
        # shellcheck source=/dev/null
        source /etc/default/travel-router 2>/dev/null || true

        clear
        _box_top
        _cl "  ${BOLD}${C}Feature Flags${NC}"
        _box_sep
        local i=1
        for flag in "${flag_list[@]}"; do
            _cl "  [$(printf '%2d' $i)] $(_dot "$flag") ${flag}"
            (( i++ )) || true
        done
        _box_sep
        _bl "  Enter number to toggle, [q] to return: "
        _box_bot

        local choice
        read -r choice
        case "$choice" in
            q|Q) return ;;
            [1-9]|1[0-9])
                local idx=$(( choice - 1 ))
                if [[ $idx -lt ${#flag_list[@]} ]]; then
                    local target_flag="${flag_list[$idx]}"
                    local cur_val="${!target_flag:-0}" new_val
                    [[ "$cur_val" = "1" ]] && new_val="0" || new_val="1"
                    if grep -q "^${target_flag}=" /etc/default/travel-router 2>/dev/null; then
                        sed -i "s/^${target_flag}=.*/${target_flag}=\"${new_val}\"/" \
                            /etc/default/travel-router
                    else
                        printf '\n%s="%s"\n' "${target_flag}" "${new_val}" >> /etc/default/travel-router
                    fi
                    printf "  ${G}✓${NC} %s → %s\n" "$target_flag" "$new_val"

                    case "$target_flag" in
                        ENABLE_VPN_KILLSWITCH|ENABLE_TOR_TRANSPARENT|ENABLE_BLOCKLISTS|ENABLE_PER_DEVICE_VPN)
                            printf "  Reloading firewall...\n"
                            /usr/local/bin/travel-router-firewall.sh --save 2>/dev/null \
                                && printf "  ${G}✓ firewall reloaded${NC}\n" \
                                || printf "  ${R}✗ firewall reload failed${NC}\n"
                            if [[ "$target_flag" = "ENABLE_BLOCKLISTS" && "$new_val" = "1" ]]; then
                                printf "  Triggering blocklist update...\n"
                                systemctl start update-blocklists.service 2>/dev/null || true
                            fi
                            ;;
                        ENABLE_DOT)
                            [[ "$new_val" = "1" ]] \
                                && systemctl restart stubby 2>/dev/null || true \
                                || systemctl stop stubby 2>/dev/null || true
                            systemctl reload-or-restart dnsmasq 2>/dev/null || true
                            printf "  ${G}✓ DoT %s${NC}\n" "$([[ $new_val = 1 ]] && echo enabled || echo disabled)"
                            ;;
                        ENABLE_ADGUARD)
                            [[ "$new_val" = "1" ]] \
                                && systemctl restart adguard-home 2>/dev/null || true \
                                || systemctl stop adguard-home 2>/dev/null || true
                            systemctl reload-or-restart dnsmasq 2>/dev/null || true
                            printf "  ${G}✓ AdGuard Home %s${NC}\n" "$([[ $new_val = 1 ]] && echo enabled || echo disabled)"
                            ;;
                        ENABLE_AVAHI_REFLECTOR)
                            systemctl reload-or-restart avahi-daemon 2>/dev/null || true
                            printf "  ${G}✓ avahi-daemon restarted${NC}\n"
                            ;;
                        ENABLE_HTTP_UA_REWRITE)
                            [[ "$new_val" = "1" ]] \
                                && systemctl restart privoxy 2>/dev/null || true \
                                || systemctl stop privoxy 2>/dev/null || true
                            printf "  ${G}✓ privoxy %s${NC}\n" "$([[ $new_val = 1 ]] && echo started || echo stopped)"
                            ;;
                        ENABLE_AP_SCHEDULE)
                            if [[ "$new_val" = "1" ]]; then
                                systemctl enable --now ap-disable.timer ap-enable.timer 2>/dev/null || true
                                printf "  ${G}✓ AP schedule timers enabled${NC}\n"
                            else
                                systemctl disable --now ap-disable.timer ap-enable.timer 2>/dev/null || true
                                printf "  ${G}✓ AP schedule timers disabled${NC}\n"
                            fi
                            ;;
                        ENABLE_CAKE_AUTOTUNE)
                            if [[ "$new_val" = "1" ]]; then
                                systemctl enable --now tune-cake.timer 2>/dev/null || true
                                printf "  ${G}✓ CAKE autotune enabled${NC}\n"
                            else
                                systemctl disable --now tune-cake.timer 2>/dev/null || true
                                printf "  ${G}✓ CAKE autotune disabled${NC}\n"
                            fi
                            ;;
                        ENABLE_CLIENT_QOS)
                            /usr/local/bin/apply-cake.sh 2>/dev/null || true
                            printf "  ${G}✓ CAKE qdisc re-applied${NC}\n"
                            ;;
                        ENABLE_AUTO_UPDATES)
                            if [[ "$new_val" = "1" ]]; then
                                systemctl enable unattended-upgrades 2>/dev/null || true
                            else
                                systemctl disable unattended-upgrades 2>/dev/null || true
                            fi
                            printf "  ${G}✓ unattended-upgrades %s${NC}\n" "$([[ $new_val = 1 ]] && echo enabled || echo disabled)"
                            ;;
                        ENABLE_UPS_MONITOR)
                            if [[ "$new_val" = "1" ]]; then
                                systemctl enable --now ups-monitor.timer 2>/dev/null || true
                                printf "  ${G}✓ UPS monitor enabled${NC}\n"
                            else
                                systemctl disable --now ups-monitor.timer 2>/dev/null || true
                                printf "  ${G}✓ UPS monitor disabled${NC}\n"
                            fi
                            ;;
                        ENABLE_2FA)
                            if [[ "$new_val" = "1" ]]; then
                                printf "  ${G}✓ 2FA enabled in config${NC}\n"
                                printf "  ${DIM}Run: sudo setup-2fa.sh  to generate your TOTP secret and QR code${NC}\n"
                            else
                                printf "  ${G}✓ 2FA disabled${NC}\n"
                                printf "  ${DIM}PAM config still loads the module — remove ~/.google_authenticator to fully disable${NC}\n"
                            fi
                            ;;
                        ENABLE_WAN_METRICS)
                            if [[ "$new_val" = "1" ]]; then
                                systemctl enable --now wan-metrics.timer 2>/dev/null || true
                                printf "  ${G}✓ WAN metrics collection enabled${NC}\n"
                            else
                                systemctl disable --now wan-metrics.timer 2>/dev/null || true
                                printf "  ${G}✓ WAN metrics collection disabled${NC}\n"
                            fi
                            ;;
                        ENABLE_PROMETHEUS_EXPORTER)
                            if [[ "$new_val" = "1" ]]; then
                                systemctl enable --now prometheus-node-exporter 2>/dev/null || true
                                printf "  ${G}✓ Prometheus exporter enabled${NC}\n"
                            else
                                systemctl disable --now prometheus-node-exporter 2>/dev/null || true
                                printf "  ${G}✓ Prometheus exporter disabled${NC}\n"
                            fi
                            ;;
                        *)
                            printf "  ${DIM}Flag saved — restart affected service if needed${NC}\n"
                            ;;
                    esac
                    sleep 2
                fi
                ;;
        esac
    done
}

# ── Logs ──────────────────────────────────────────────────────────────────────
show_logs() {
    while true; do
        clear
        _box_top
        _cl "  ${BOLD}${C}Logs${NC}"
        _box_sep
        _bl "  [1] WAN watchdog (last 30 lines)"
        _bl "  [2] Tailscale journal (last 30)"
        _bl "  [3] Failover watchdog journal (last 30)"
        _bl "  [4] hostapd journal (last 30)"
        _bl "  [5] Failed systemd units"
        _bl "  [6] update-router.log (last 20)"
        _box_sep
        _bl "  Enter choice, [q] to return: "
        _box_bot

        local choice
        read -r choice
        case "$choice" in
            1) clear; printf "${W}wan-watchdog.log:${NC}\n\n"
               tail -n 30 /var/log/wan-watchdog.log 2>/dev/null || printf "  (no log found)\n"
               printf "\n  Press any key..."; read -rsn1 || true ;;
            2) clear; printf "${W}tailscaled (last 30):${NC}\n\n"
               journalctl -u tailscaled -n 30 --no-pager 2>/dev/null || printf "  (unavailable)\n"
               printf "\n  Press any key..."; read -rsn1 || true ;;
            3) clear; printf "${W}failover-watchdog (last 30):${NC}\n\n"
               journalctl -u failover-watchdog -n 30 --no-pager 2>/dev/null \
                   || printf "  (unavailable)\n"
               printf "\n  Press any key..."; read -rsn1 || true ;;
            4) clear; printf "${W}hostapd (last 30):${NC}\n\n"
               journalctl -u hostapd -n 30 --no-pager 2>/dev/null \
                   || printf "  (unavailable)\n"
               printf "\n  Press any key..."; read -rsn1 || true ;;
            5) clear; printf "${W}Failed units:${NC}\n\n"
               systemctl --failed --no-pager 2>/dev/null || true
               printf "\n  Press any key..."; read -rsn1 || true ;;
            6) clear; printf "${W}update-router.log:${NC}\n\n"
               tail -n 20 /var/log/update-router.log 2>/dev/null || printf "  (no log found)\n"
               printf "\n  Press any key..."; read -rsn1 || true ;;
            q|Q) return ;;
        esac
    done
}

# ── Clients ───────────────────────────────────────────────────────────────────
show_clients() {
    clear
    _box_top
    _cl "  ${BOLD}${C}AP Clients${NC}"
    _box_sep

    local station_dump client_count
    station_dump=$(iw dev uap0 station dump 2>/dev/null || true)
    client_count=$(printf '%s' "$station_dump" | grep -c "^Station" || true)

    if [[ "${client_count:-0}" -eq 0 ]]; then
        _bl "  No clients connected."
        _box_sep
        _bl "  [r] Refresh  [q] Return: "
        _box_bot
        local c; read -r c
        [[ "$c" = "r" || "$c" = "R" ]] && show_clients
        return
    fi

    _cl "  ${DIM}MAC               IP               Hostname         Signal${NC}"
    local _mac="" _ip="" _signal="?" _pending=0
    while IFS= read -r _line; do
        case "$_line" in
            "Station "*)
                # flush previous station if we have one
                if [[ "$_pending" -eq 1 ]]; then
                    local _hostname=""
                    _hostname=$(awk -v ip="$_ip" '$3==ip && $4!="*" {print $4; exit}' \
                        /var/lib/misc/dnsmasq.leases 2>/dev/null || true)
                    local _host_display=""
                    [[ -n "$_hostname" ]] && _host_display=" ${DIM}${_hostname}${NC}"
                    _cl "  ${G}${_mac}${NC}  ${DIM}${_ip}${NC}$(printf '%*s' $(( 18 - ${#_ip} )) '')${_host_display}$(printf '%*s' $(( 17 - ${#_hostname} )) '')${_signal}"
                fi
                _mac=$(printf '%s' "$_line" | awk '{print $2}')
                _ip=$(ip neigh show dev uap0 2>/dev/null \
                    | awk -v m="$_mac" 'tolower($3)==tolower(m){print $1; exit}')
                [[ -z "$_ip" ]] && _ip="unknown"
                _signal="?"
                _pending=1
                ;;
            *"signal:"*)
                _signal=$(printf '%s' "$_line" | awk '{print $2, $3}')
                ;;
        esac
    done <<< "$station_dump"
    # flush last station
    if [[ "$_pending" -eq 1 ]]; then
        local _hostname=""
        _hostname=$(awk -v ip="$_ip" '$3==ip && $4!="*" {print $4; exit}' \
            /var/lib/misc/dnsmasq.leases 2>/dev/null || true)
        local _host_display=""
        [[ -n "$_hostname" ]] && _host_display=" ${DIM}${_hostname}${NC}"
        _cl "  ${G}${_mac}${NC}  ${DIM}${_ip}${NC}$(printf '%*s' $(( 18 - ${#_ip} )) '')${_host_display}$(printf '%*s' $(( 17 - ${#_hostname} )) '')${_signal}"
    fi

    _box_sep
    _bl "  [r] Refresh  [q] Return: "
    _box_bot
    local _choice; read -r _choice
    [[ "$_choice" = "r" || "$_choice" = "R" ]] && show_clients
}

# ── Network tools ─────────────────────────────────────────────────────────────
show_network() {
    while true; do
        clear
        _box_top
        _cl "  ${BOLD}${C}Network Tools${NC}"
        _box_sep
        # shellcheck source=/dev/null
        source /etc/default/travel-router 2>/dev/null || true
        local bt_mac="${IPHONE_BT_MAC:-}"
        local bt_hint; bt_hint="${bt_mac:+${bt_mac}}"; bt_hint="${bt_hint:-(set IPHONE_BT_MAC in Settings first)}"
        _bl "  [1] Show WiFi QR code"
        _bl "  [2] Run speedtest + update CAKE bandwidth (~30s)"
        _bl "  [3] Clone MAC to wlan0 (captive portal bypass)"
        _bl "  [4] Restore original wlan0 MAC"
        _bl "  [5] Real-time bandwidth monitor (bmon)"
        _bl "  [6] Per-connection traffic inspector (iftop)"
        _bl "  [7] Connect to hotel / new WiFi network"
        _cl "  [8] Start Bluetooth tethering  ${DIM}${bt_hint}${NC}"
        _bl "  [9] Stop Bluetooth tethering"
        _bl "  [h] Re-check captive portal now"
        _box_sep
        _bl "  Enter choice, [q] to return: "
        _box_bot

        local choice
        read -r choice
        case "$choice" in
            1) clear; printf "${W}WiFi QR Code:${NC}\n\n"
               local _ssid _pass _auth
               _ssid=$(grep "^ssid=" /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2)
               _pass=$(grep "^wpa_passphrase=" /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2)
               _auth="WPA"
               [[ -z "$_pass" ]] && _auth="nopass"
               local _wifi_str="WIFI:T:${_auth};S:${_ssid};P:${_pass};;"
               printf "  Network: %s\n  Password: %s\n\n" "$_ssid" "${_pass:-(open)}"
               if command -v qrencode >/dev/null 2>&1; then
                   qrencode -t ansiutf8 "$_wifi_str" 2>/dev/null || printf "  (qrencode failed)\n"
               else
                   printf "  qrencode not installed — scan this string manually:\n  %s\n" "$_wifi_str"
               fi
               printf "\n  Press any key..."; read -rsn1 || true ;;
            2) clear; printf "${W}Running speedtest...${NC}\n\n"
               /usr/local/bin/tune-cake.sh 2>&1 || \
                   printf "  ${R}failed${NC} — check: journalctl -u tune-cake\n"
               printf "\n  Press any key..."; read -rsn1 || true ;;
            3) printf "\n  MAC to clone (e.g. aa:bb:cc:dd:ee:ff): "
               local mac_in; read -r mac_in
               if [[ "$mac_in" =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]]; then
                   /usr/local/bin/clone-mac.sh "$mac_in" 2>&1 \
                       && printf "  ${G}✓ MAC cloned to wlan0${NC}\n" \
                       || printf "  ${R}✗ clone failed${NC}\n"
               else
                   printf "  ${R}Invalid MAC format${NC}\n"
               fi
               sleep 2 ;;
            4) /usr/local/bin/clone-mac.sh --restore 2>&1 \
                   && printf "  ${G}✓ original MAC restored${NC}\n" \
                   || printf "  ${R}✗ restore failed${NC}\n"
               sleep 2 ;;
            5) command -v bmon >/dev/null 2>&1 && bmon \
                   || { printf "  bmon not installed\n  Press any key..."; read -rsn1 || true; } ;;
            6) command -v iftop >/dev/null 2>&1 && iftop -i uap0 2>/dev/null || true \
                   || { printf "  iftop not installed\n  Press any key..."; read -rsn1 || true; } ;;
            7) clear
               printf "${W}Connect to WiFi Network${NC}\n\n"
               printf "  Scanning...\n\n"
               nmcli --fields SSID,SIGNAL,SECURITY device wifi list 2>/dev/null | head -20 || true
               printf "\n  SSID to connect to (Enter to cancel): "
               local wifi_ssid; read -r wifi_ssid
               [[ -z "$wifi_ssid" ]] && continue
               printf "  Password (Enter for open network): "
               local wifi_pass; read -rs wifi_pass; printf "\n"
               local _connect_ok=0
               if [[ -n "$wifi_pass" ]]; then
                   nmcli device wifi connect "$wifi_ssid" password "$wifi_pass" ifname wlan0 2>&1 \
                       && { printf "  ${G}✓ connected to %s${NC}\n" "$wifi_ssid"; _connect_ok=1; } \
                       || printf "  ${R}✗ failed — check SSID/password${NC}\n"
               else
                   nmcli device wifi connect "$wifi_ssid" ifname wlan0 2>&1 \
                       && { printf "  ${G}✓ connected to %s${NC}\n" "$wifi_ssid"; _connect_ok=1; } \
                       || printf "  ${R}✗ could not connect${NC}\n"
               fi
               if [[ "$_connect_ok" -eq 1 ]]; then
                   printf "  ${DIM}Waiting for DHCP...${NC}\n"
                   sleep 4
                   printf "  Checking for captive portal...\n"
                   /usr/local/bin/captive-check.sh 2>/dev/null || true
                   if [ -f /tmp/captive-portal-active ]; then
                       printf "  ${R}${BOLD}⚠  Captive portal detected!${NC}\n"
                       printf "  ${W}Open a browser on your laptop/phone and log in to the hotel WiFi,${NC}\n"
                       printf "  ${W}then return here and press [h] to re-check.${NC}\n"
                   else
                       printf "  ${G}✓ Internet clear — no captive portal${NC}\n"
                   fi
               fi
               printf "\n  Press any key..."; read -rsn1 || true ;;
            8) if [[ -z "$bt_mac" ]]; then
                   printf "  ${R}IPHONE_BT_MAC not set — go to Settings → [1]${NC}\n"
                   sleep 2
               else
                   printf "  Starting Bluetooth tethering to %s...\n" "$bt_mac"
                   /usr/local/bin/start-bt-tether.sh "$bt_mac" 2>&1 \
                       && printf "  ${G}✓ BT tethering started${NC}\n" \
                       || printf "  ${R}✗ BT tethering failed — is the phone paired and hotspot on?${NC}\n"
                   sleep 2
               fi ;;
            9) printf "  Stopping Bluetooth tethering...\n"
               /usr/local/bin/stop-bt-tether.sh 2>&1 \
                   && printf "  ${G}✓ BT tethering stopped${NC}\n" \
                   || printf "  ${R}✗ stop failed${NC}\n"
               sleep 2 ;;
            h|H) printf "  Running captive portal check...\n"
               /usr/local/bin/captive-check.sh 2>/dev/null || true
               if [ -f /tmp/captive-portal-active ]; then
                   printf "  ${R}⚠  Portal still active — authenticate via your browser first${NC}\n"
               else
                   printf "  ${G}✓ Internet clear — no captive portal${NC}\n"
               fi
               sleep 3 ;;
            q|Q) return ;;
        esac
    done
}

# ── Settings ──────────────────────────────────────────────────────────────────
show_settings() {
    while true; do
        clear
        # shellcheck source=/dev/null
        source /etc/default/travel-router 2>/dev/null || true

        local ap_ssid ap_pass_display ts_args_disp
        ap_ssid=$(grep "^ssid=" /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2 || echo "?")
        local ap_raw_pass
        ap_raw_pass=$(grep "^wpa_passphrase=" /etc/hostapd/hostapd.conf 2>/dev/null | head -1 | cut -d= -f2 || echo "")
        [[ -n "$ap_raw_pass" ]] \
            && ap_pass_display="(set — ${#ap_raw_pass} chars)" \
            || ap_pass_display="(empty)"

        ts_args_disp="${TAILSCALE_UP_ARGS:-}"
        [[ ${#ts_args_disp} -gt 38 ]] && ts_args_disp="${ts_args_disp:0:35}..."

        _box_top
        _cl "  ${BOLD}${C}⚙  Settings${NC}"
        _box_sep
        _cl "  ${DIM}TETHERING & ACCESS POINT${NC}"
        _cl "  [1] iPhone Bluetooth MAC     ${DIM}${IPHONE_BT_MAC:-(empty — pair first)}${NC}"
        _cl "  [2] AP Network Name          ${DIM}${ap_ssid}${NC}"
        _cl "  [3] AP Password              ${DIM}${ap_pass_display}${NC}"
        _be
        _cl "  ${DIM}NOTIFICATIONS & VPN${NC}"
        _cl "  [4] ntfy.sh Topic            ${DIM}${NTFY_TOPIC:-(empty)}${NC}"
        _cl "  [5] Headscale URL            ${DIM}${HEADSCALE_URL:-(using Tailscale cloud)}${NC}"
        _cl "  [6] Tailscale Arguments      ${DIM}${ts_args_disp}${NC}"
        _be
        _cl "  ${DIM}TRAFFIC & ROUTING${NC}"
        _cl "  [7] WAN Ping Targets         ${DIM}${WAN_PING_TARGETS:-1.1.1.1 8.8.8.8}${NC}"
        _cl "  [8] VPN Device MACs          ${DIM}${VPN_DEVICE_MACS:-(empty — all devices)}${NC}"
        _cl "  [9] Split Tunnel Domains     ${DIM}${SPLIT_TUNNEL_DOMAINS:-(empty)}${NC}"
        _cl "  [0] Per-Client Bandwidth     ${DIM}${AP_CLIENT_BANDWIDTH:-unlimited}${NC}"
        _cl "  [b] Tor AP Password          ${DIM}${TOR_AP_PASS:+(set — ${#TOR_AP_PASS} chars)}${TOR_AP_PASS:-(empty)}${NC}"
        _cl "  [c] Max Blocklist Entries    ${DIM}${MAX_BLOCKLIST_ENTRIES:-500000}${NC}"
        _be
        _cl "  ${DIM}AP SCHEDULING${NC}"
        _cl "  [d] AP Disable Time          ${DIM}${AP_DISABLE_TIME:-23:00}${NC}"
        _cl "  [e] AP Enable Time           ${DIM}${AP_ENABLE_TIME:-07:00}${NC}"
        _be
        _cl "  ${DIM}MONITORING${NC}"
        _cl "  [f] UPS Shutdown Threshold % ${DIM}${UPS_SHUTDOWN_THRESHOLD:-15}${NC}"
        _cl "  [g] Prometheus Pushgw URL    ${DIM}${PUSHGW_URL:-(empty — push disabled)}${NC}"
        _be
        local ssh_key_disp
        [[ -n "${SSH_ADMIN_KEY:-}" ]] \
            && ssh_key_disp="(set — ${SSH_ADMIN_KEY:0:28}...)" \
            || ssh_key_disp="(empty — password auth active)"
        _cl "  ${DIM}SECURITY${NC}"
        _cl "  [a] SSH Admin Key            ${DIM}${ssh_key_disp}${NC}"
        _box_sep
        _bl "  Enter number/letter to edit, [q] to return: "
        _box_bot

        local choice
        read -r choice
        case "$choice" in
            q|Q) return ;;
            1) _cfg_edit IPHONE_BT_MAC \
                "iPhone Bluetooth MAC  (pair via: bluetoothctl pair <MAC>)"; sleep 1 ;;
            2) _ap_edit_ssid; sleep 1 ;;
            3) _ap_edit_pass; sleep 1 ;;
            4) _cfg_edit NTFY_TOPIC \
                "ntfy.sh Topic  (subscribe in the ntfy app, pick a secret name)"; sleep 1 ;;
            5) _cfg_edit HEADSCALE_URL \
                "Headscale URL  (e.g. http://203.0.113.5:8080, blank = Tailscale cloud)"; sleep 1 ;;
            6) _cfg_edit TAILSCALE_UP_ARGS \
                "Tailscale Up Arguments"; sleep 1 ;;
            7) _cfg_edit WAN_PING_TARGETS \
                "WAN Ping Targets  (space-separated IPs)"; sleep 1 ;;
            8) _cfg_edit VPN_DEVICE_MACS \
                "VPN Device MACs  (space-separated, empty = all devices use VPN)"; sleep 1 ;;
            9) _cfg_edit SPLIT_TUNNEL_DOMAINS \
                "Split Tunnel Domains  (space-separated, requires ENABLE_SPLIT_TUNNEL=1)"; sleep 1 ;;
            0) _cfg_edit AP_CLIENT_BANDWIDTH \
                "Per-Client Bandwidth  (e.g. 5mbit, 2mbit, or 'unlimited')"; sleep 1 ;;
            b|B) _cfg_edit TOR_AP_PASS \
                "Tor AP Password  (min 8 chars, used for the uap1 Tor SSID)" 1; sleep 1 ;;
            c|C) _cfg_edit MAX_BLOCKLIST_ENTRIES \
                "Max Blocklist Entries  (default 500000, lower to save RAM)"; sleep 1 ;;
            d|D) _cfg_edit AP_DISABLE_TIME \
                "AP Disable Time  (HH:MM, requires ENABLE_AP_SCHEDULE=1)"; sleep 1 ;;
            e|E) _cfg_edit AP_ENABLE_TIME \
                "AP Enable Time  (HH:MM, requires ENABLE_AP_SCHEDULE=1)"; sleep 1 ;;
            f|F) _cfg_edit UPS_SHUTDOWN_THRESHOLD \
                "UPS Shutdown Threshold  (battery % to trigger safe shutdown)"; sleep 1 ;;
            g|G) _cfg_edit PUSHGW_URL \
                "Prometheus Pushgateway URL  (e.g. http://192.168.1.10:9091)"; sleep 1 ;;
            a|A)
                _cfg_edit SSH_ADMIN_KEY "SSH Admin Public Key  (ssh-ed25519 AAAA... or ssh-rsa AAAA...)"
                if [[ -n "${SSH_ADMIN_KEY:-}" ]]; then
                    printf "  Appending key to authorized_keys...\n"
                    mkdir -p /root/.ssh
                    chmod 700 /root/.ssh
                    if ! grep -qF "${SSH_ADMIN_KEY}" /root/.ssh/authorized_keys 2>/dev/null; then
                        printf '%s\n' "${SSH_ADMIN_KEY}" >> /root/.ssh/authorized_keys
                        chmod 600 /root/.ssh/authorized_keys
                        printf "  ${G}✓ Key added to /root/.ssh/authorized_keys${NC}\n"
                    else
                        printf "  ${DIM}Key already present in authorized_keys${NC}\n"
                    fi
                fi
                sleep 2 ;;
        esac
    done
}

# ── System ────────────────────────────────────────────────────────────────────
show_system() {
    while true; do
        clear
        _box_top
        _cl "  ${BOLD}${C}System Actions${NC}"
        _box_sep
        _bl "  [0] Change root password"
        _bl "  [1] Reboot now"
        _bl "  [2] Shutdown now"
        _bl "  [3] Run update-router.sh"
        _bl "  [4] Send daily digest now"
        _bl "  [5] Reload firewall"
        _bl "  [6] Run travel-diagnostic (collect logs)"
        _bl "  [7] Set up 2FA / TOTP (setup-2fa.sh)"
        _bl "  [8] Update threat-intel blocklists now"
        _bl "  [9] Generate bandwidth report now"
        _box_sep
        _bl "  Enter choice, [q] to return: "
        _box_bot

        local choice
        read -r choice
        case "$choice" in
            q|Q) return ;;
            0) printf "\n  New root password: "
               local _pw1; read -rs _pw1; printf "\n"
               printf "  Confirm password: "
               local _pw2; read -rs _pw2; printf "\n"
               if [[ -z "$_pw1" ]]; then
                   printf "  ${DIM}Cancelled${NC}\n"
               elif [[ "$_pw1" != "$_pw2" ]]; then
                   printf "  ${R}✗ Passwords do not match${NC}\n"
               elif [[ ${#_pw1} -lt 8 ]]; then
                   printf "  ${R}✗ Must be at least 8 characters${NC}\n"
               else
                   printf '%s:%s' "root" "$_pw1" | chpasswd 2>/dev/null \
                       && printf "  ${G}✓ Root password updated${NC}\n" \
                       || printf "  ${R}✗ chpasswd failed${NC}\n"
               fi
               sleep 2 ;;
            1) printf "  Rebooting...\n"; reboot ;;
            2) printf "  Shutting down...\n"; shutdown -h now ;;
            3) clear; printf "${W}Running update-router.sh...${NC}\n\n"
               /usr/local/bin/update-router.sh 2>&1 || true
               printf "\n  Press any key..."; read -rsn1 || true ;;
            4) /usr/local/bin/daily-digest.sh 2>&1 || true
               printf "  ${G}✓ digest sent${NC}\n"; sleep 2 ;;
            5) printf "  Reloading firewall...\n"
               /usr/local/bin/travel-router-firewall.sh --save 2>/dev/null || true
               printf "  ${G}✓ firewall reloaded${NC}\n"; sleep 2 ;;
            6) clear; printf "${W}Running travel-diagnostic...${NC}\n\n"
               /usr/local/bin/travel-diagnostic 2>&1 || true
               printf "\n  Press any key..."; read -rsn1 || true ;;
            7) clear; /usr/local/bin/setup-2fa.sh 2>&1 || true
               printf "\n  Press any key..."; read -rsn1 || true ;;
            8) clear; printf "${W}Updating blocklists...${NC}\n\n"
               /usr/local/bin/update-blocklists.sh 2>&1 || true
               printf "\n  Press any key..."; read -rsn1 || true ;;
            9) clear; printf "${W}Generating bandwidth report...${NC}\n\n"
               /usr/local/bin/generate-bandwidth-report.sh 2>&1 || true
               printf "\n  Report: /var/lib/travel-router/bandwidth.html\n"
               printf "\n  Press any key..."; read -rsn1 || true ;;
        esac
    done
}

# ── Main loop ─────────────────────────────────────────────────────────────────
while true; do
    # shellcheck source=/dev/null
    source /etc/default/travel-router 2>/dev/null || true
    draw_dashboard
    key=""
    read -rsn1 -t 5 key || true
    case "$key" in
        1) show_services  ;;
        2) show_features  ;;
        3) show_logs      ;;
        4) show_clients   ;;
        5) show_network   ;;
        6) show_settings  ;;
        7) show_system    ;;
        q|Q) _cleanup     ;;
    esac
done
