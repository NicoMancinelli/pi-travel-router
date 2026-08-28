#!/bin/bash
# /usr/local/bin/tailscale-exit-node.sh — Tailscale Exit Node Management Tool
#
# Commands:
#   status             Show active exit node, local IP, and tailnet status
#   list [--json]      List available exit node peers advertising in the tailnet
#   set <node>         Set active exit node (hostname, IP, or ID) with LAN access allowed
#   clear | off        Disable exit node routing (revert to direct WAN)
#   advertise [on|off] Toggle or set exit node advertisement for this router
#
set -euo pipefail

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; C='\033[0;36m'; NC='\033[0m'; BLD='\033[1m'
ok()   { echo -e "${G}✓${NC} $*"; }
info() { echo -e "${C}→${NC} $*"; }
warn() { echo -e "${Y}⚠${NC} $*"; }
die()  { echo -e "${R}✗ FATAL:${NC} $*" >&2; exit 1; }

usage() {
    cat << USAGE
Usage: tailscale-exit-node.sh <command> [args]

Commands:
  status               Show current exit node status and routing
  list [--json]        List all exit node candidates on the tailnet
  set <node>           Route all router traffic through specified exit node
  clear | off          Clear active exit node and restore direct WAN routing
  advertise [on|off]   Enable/disable advertising this router as an exit node
  help                 Show this help message
USAGE
    exit 0
}

check_tailscale() {
    command -v tailscale >/dev/null 2>&1 || die "tailscale CLI is not installed"
    local backend
    backend=$(tailscale status --json 2>/dev/null | jq -r '.BackendState // "Down"' 2>/dev/null || echo "Down")
    if [[ "$backend" != "Running" ]]; then
        die "Tailscale is not running (state: $backend)"
    fi
}

cmd_status() {
    check_tailscale
    local ts_json
    ts_json=$(tailscale status --json 2>/dev/null) || die "Failed to query tailscale status"

    local self_name self_ip exit_node_name exit_node_ip
    self_name=$(echo "$ts_json" | jq -r '.Self.HostName // "unknown"')
    self_ip=$(echo "$ts_json" | jq -r '.Self.TailscaleIPs[0] // "none"')
    
    # Active exit node
    exit_node_name=$(echo "$ts_json" | jq -r '.ExitNodeStatus.HostName // empty')
    exit_node_ip=$(echo "$ts_json" | jq -r '.ExitNodeStatus.TailscaleIPs[0] // empty')
    
    local advertising
    advertising=$(echo "$ts_json" | jq -r '.Self.ExitNodeOption // false')

    echo -e "${BLD}Tailscale Node:${NC} $self_name ($self_ip)"
    if [[ "$advertising" == "true" ]]; then
        echo -e "${BLD}Advertising as Exit Node:${NC} ${G}Yes${NC}"
    else
        echo -e "${BLD}Advertising as Exit Node:${NC} No"
    fi

    if [[ -n "$exit_node_name" || -n "$exit_node_ip" ]]; then
        echo -e "${BLD}Active Exit Node:${NC} ${G}● Active${NC} — ${BLD}${exit_node_name:-$exit_node_ip}${NC} ($exit_node_ip)"
        echo -e "${C}→ All AP and router traffic is currently exiting through Tailscale.${NC}"
    else
        echo -e "${BLD}Active Exit Node:${NC} ${Y}○ Direct WAN (None)${NC}"
        echo -e "→ Traffic is exiting directly through local WAN uplink."
    fi
}

cmd_list() {
    check_tailscale
    local as_json=0
    if [[ "${1:-}" == "--json" ]]; then
        as_json=1
    fi

    local ts_json
    ts_json=$(tailscale status --json 2>/dev/null) || die "Failed to query tailscale status"

    if [[ $as_json -eq 1 ]]; then
        echo "$ts_json" | jq '[.Peer // {} | to_entries[] | .value | select(.ExitNodeOption == true) | {
            id: .ID,
            hostname: .HostName,
            dns_name: .DNSName,
            tailscale_ip: (.TailscaleIPs[0] // ""),
            online: .Online,
            active_exit_node: (.ExitNode // false),
            os: .OS
        }]'
        return 0
    fi

    echo -e "${BLD}Available Tailscale Exit Nodes:${NC}"
    printf "%-20s %-18s %-10s %-8s %s\n" "HOSTNAME" "TAILSCALE IP" "STATUS" "ACTIVE" "OS"
    printf "%-20s %-18s %-10s %-8s %s\n" "--------------------" "------------------" "----------" "--------" "--"

    # shellcheck disable=SC2016
    local rows
    rows=$(echo "$ts_json" | jq -r '.Peer // {} | to_entries[] | .value | select(.ExitNodeOption == true) | 
        "\(.HostName)\t\(.TailscaleIPs[0] // "")\t\(if .Online then "online" else "offline" end)\t\(if .ExitNode then "ACTIVE" else "-" end)\t\(.OS // "")"' 2>/dev/null || true)

    if [[ -z "$rows" ]]; then
        echo "  (No peers are currently advertising as exit nodes on your tailnet)"
        return 0
    fi

    while IFS=$'\t' read -r hname ip status active os; do
        [[ -z "$hname" ]] && continue
        local status_str active_str
        if [[ "$status" == "online" ]]; then
            status_str="${G}online${NC}"
        else
            status_str="${R}offline${NC}"
        fi
        if [[ "$active" == "ACTIVE" ]]; then
            active_str="${G}● YES${NC}"
        else
            active_str="○ no"
        fi
        printf "%-20s %-18s %-19b %-16b %s\n" "$hname" "$ip" "$status_str" "$active_str" "$os"
    done <<< "$rows"
}

cmd_set() {
    [[ $# -lt 1 ]] && die "Usage: tailscale-exit-node.sh set <node-name-or-ip>"
    check_tailscale
    local target="$1"

    info "Setting Tailscale exit node to: $target (with LAN access allowed)..."
    tailscale set --exit-node="$target" --exit-node-allow-lan-access=true || die "Failed to set exit node"
    ok "Exit node set to $target with LAN access preserved."
}

cmd_clear() {
    check_tailscale
    info "Clearing Tailscale exit node..."
    tailscale set --exit-node= || die "Failed to clear exit node"
    ok "Exit node cleared. Traffic now routes directly through local WAN."
}

cmd_advertise() {
    check_tailscale
    local mode="${1:-}"
    if [[ "$mode" == "on" || "$mode" == "true" || "$mode" == "1" ]]; then
        info "Enabling exit node advertisement on this router..."
        tailscale set --advertise-exit-node=true || die "Failed to enable exit node advertisement"
        ok "This router is now advertising as an exit node on your tailnet."
    elif [[ "$mode" == "off" || "$mode" == "false" || "$mode" == "0" ]]; then
        info "Disabling exit node advertisement on this router..."
        tailscale set --advertise-exit-node=false || die "Failed to disable exit node advertisement"
        ok "Exit node advertisement disabled."
    else
        # Toggle current state
        local current
        current=$(tailscale status --json 2>/dev/null | jq -r '.Self.ExitNodeOption // false' 2>/dev/null || echo "false")
        if [[ "$current" == "true" ]]; then
            cmd_advertise "off"
        else
            cmd_advertise "on"
        fi
    fi
}

# ── Main Dispatcher ───────────────────────────────────────────────────────────
CMD="${1:-status}"
shift || true

case "$CMD" in
    status)         cmd_status "$@" ;;
    list|ls)        cmd_list "$@" ;;
    set)            cmd_set "$@" ;;
    clear|off|unset) cmd_clear "$@" ;;
    advertise|adv)  cmd_advertise "$@" ;;
    help|-h|--help) usage ;;
    *)              die "Unknown command: $CMD (run with 'help' for usage)" ;;
esac
