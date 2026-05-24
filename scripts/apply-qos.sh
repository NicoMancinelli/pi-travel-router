#!/bin/bash
# apply-qos.sh — per-device bandwidth limits using tc HTB + iptables MARK
# Usage: apply-qos.sh <iface> <mac> <down_kbps> <up_kbps>   — set limit
#        apply-qos.sh <iface> <mac> clear                    — remove limit
#        apply-qos.sh list                                    — print JSON
#        apply-qos.sh clear-all                              — remove all QoS rules

set -euo pipefail

QOS_STORE="/var/lib/travel-router/qos-limits.json"
MARK_BASE=100   # fwmark offset to avoid collisions with per-device VPN (0x64=100)

# Graceful exit if tc or iptables are not available (e.g. in testing)
if ! command -v tc &>/dev/null || ! command -v iptables &>/dev/null; then
    exit 0
fi

# ── helpers ───────────────────────────────────────────────────────────────────

_store_read() {
    if [[ -f "$QOS_STORE" ]]; then
        cat "$QOS_STORE"
    else
        echo "[]"
    fi
}

_store_write() {
    local json="$1"
    mkdir -p "$(dirname "$QOS_STORE")"
    printf '%s\n' "$json" > "$QOS_STORE"
}

# Convert MAC aa:bb:cc:dd:ee:ff → decimal mark (last two octets XOR'd to
# produce a value 200-455 so it never overlaps with MARK_BASE 100..199)
_mac_to_mark() {
    local mac="$1"
    local lo lo2
    lo=$(printf '%d' "0x${mac:12:2}")
    lo2=$(printf '%d' "0x${mac:15:2}")
    # Combine both last octets, add MARK_BASE+100 to stay above 200
    echo $(( MARK_BASE + 100 + (lo * 256 + lo2) % 500 + 1 ))
}

_setup_root_qdisc() {
    local iface="$1"
    # Add root HTB qdisc if not already present
    if ! tc qdisc show dev "$iface" 2>/dev/null | grep -q "htb 1:"; then
        # shellcheck disable=SC2086
        tc qdisc add dev "$iface" root handle 1: htb default 999 2>/dev/null || true
    fi
    # Default class (unthrottled traffic)
    tc class add dev "$iface" parent 1: classid 1:999 htb rate 1000mbit 2>/dev/null || true
}

_apply_limit() {
    local iface="$1" mac="$2" down_kbps="$3" up_kbps="$4"
    local mark
    mark=$(_mac_to_mark "$mac")

    # Ensure root qdisc exists on AP interface
    _setup_root_qdisc "$iface"

    # Download: limit traffic leaving uap0 toward the client
    tc class replace dev "$iface" parent 1: classid "1:${mark}" htb \
        rate "${down_kbps}kbit" ceil "${down_kbps}kbit" 2>/dev/null || true
    tc filter replace dev "$iface" parent 1: protocol ip \
        handle "${mark}" fw flowid "1:${mark}" 2>/dev/null || true

    # Mark packets destined for this MAC (POSTROUTING, outbound on AP iface)
    # Remove any existing rule first (idempotent)
    iptables -t mangle -D POSTROUTING -o "$iface" \
        -m mac --mac-source "$mac" -j MARK --set-mark "${mark}" 2>/dev/null || true
    # shellcheck disable=SC2086
    iptables -t mangle -A POSTROUTING -o "$iface" \
        -m mac --mac-source "$mac" -j MARK --set-mark "${mark}"

    # Upload: limit traffic arriving from the client on uap0 (ingress via IFB)
    # We use a simple PREROUTING mark + HTB on the IFB device if available.
    # If ifb0 doesn't exist, skip upload shaping silently.
    if ip link show ifb0 &>/dev/null; then
        _setup_root_qdisc ifb0
        tc class replace dev ifb0 parent 1: classid "1:${mark}" htb \
            rate "${up_kbps}kbit" ceil "${up_kbps}kbit" 2>/dev/null || true
        tc filter replace dev ifb0 parent 1: protocol ip \
            handle "${mark}" fw flowid "1:${mark}" 2>/dev/null || true
        iptables -t mangle -D PREROUTING -i "$iface" \
            -m mac --mac-source "$mac" -j MARK --set-mark "${mark}" 2>/dev/null || true
        # shellcheck disable=SC2086
        iptables -t mangle -A PREROUTING -i "$iface" \
            -m mac --mac-source "$mac" -j MARK --set-mark "${mark}"
    fi
}

_clear_limit() {
    local iface="$1" mac="$2"
    local mark
    mark=$(_mac_to_mark "$mac")

    # Remove iptables marks
    iptables -t mangle -D POSTROUTING -o "$iface" \
        -m mac --mac-source "$mac" -j MARK --set-mark "${mark}" 2>/dev/null || true
    iptables -t mangle -D PREROUTING -i "$iface" \
        -m mac --mac-source "$mac" -j MARK --set-mark "${mark}" 2>/dev/null || true

    # Remove tc classes and filters (non-fatal if not present)
    tc filter del dev "$iface" parent 1: handle "${mark}" fw 2>/dev/null || true
    tc class del dev "$iface" parent 1: classid "1:${mark}" 2>/dev/null || true
    if ip link show ifb0 &>/dev/null; then
        tc filter del dev ifb0 parent 1: handle "${mark}" fw 2>/dev/null || true
        tc class del dev ifb0 parent 1: classid "1:${mark}" 2>/dev/null || true
    fi
}

_clear_all() {
    # Read all stored limits and clear each one
    local json
    json=$(_store_read)
    python3 - <<EOF
import json, subprocess, sys
try:
    limits = json.loads("""$json""")
except Exception:
    limits = []
for l in limits:
    subprocess.run(
        ["/usr/local/sbin/apply-qos.sh", l.get("interface","uap0"), l["mac"], "clear"],
        check=False
    )
EOF
    _store_write "[]"
}

_store_upsert() {
    local mac="$1" down_kbps="$2" up_kbps="$3" iface="$4"
    local json
    json=$(_store_read)
    json=$(python3 - <<EOF
import json, sys
mac = "${mac}".lower()
data = json.loads("""$json""")
data = [e for e in data if e.get("mac","").lower() != mac]
data.append({"mac": mac, "down_kbps": int("${down_kbps}"), "up_kbps": int("${up_kbps}"), "interface": "${iface}"})
print(json.dumps(data))
EOF
)
    _store_write "$json"
}

_store_remove() {
    local mac="$1"
    local json
    json=$(_store_read)
    json=$(python3 - <<EOF
import json
mac = "${mac}".lower()
data = json.loads("""$json""")
data = [e for e in data if e.get("mac","").lower() != mac]
print(json.dumps(data))
EOF
)
    _store_write "$json"
}

# ── main dispatch ─────────────────────────────────────────────────────────────

CMD="${1:-}"

case "$CMD" in
    list)
        _store_read
        ;;
    clear-all)
        _clear_all
        ;;
    "")
        echo "Usage: apply-qos.sh <iface> <mac> <down_kbps> <up_kbps>" >&2
        echo "       apply-qos.sh <iface> <mac> clear" >&2
        echo "       apply-qos.sh list" >&2
        echo "       apply-qos.sh clear-all" >&2
        exit 1
        ;;
    *)
        # Positional: iface mac (down_kbps|clear) [up_kbps]
        IFACE="$1"
        MAC="${2:-}"
        ARG3="${3:-}"

        if [[ -z "$MAC" || -z "$ARG3" ]]; then
            echo "Usage: apply-qos.sh <iface> <mac> <down_kbps> <up_kbps>" >&2
            exit 1
        fi

        # Validate MAC format
        if ! [[ "$MAC" =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]]; then
            echo "Invalid MAC address: $MAC" >&2
            exit 1
        fi

        if [[ "$ARG3" == "clear" ]]; then
            _clear_limit "$IFACE" "$MAC"
            _store_remove "$MAC"
        else
            DOWN_KBPS="$ARG3"
            UP_KBPS="${4:-$DOWN_KBPS}"
            _apply_limit "$IFACE" "$MAC" "$DOWN_KBPS" "$UP_KBPS"
            _store_upsert "$MAC" "$DOWN_KBPS" "$UP_KBPS" "$IFACE"
        fi
        ;;
esac
