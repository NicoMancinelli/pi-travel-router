#!/usr/bin/env bats
# Unit tests for failover-watchdog.sh interface pattern matching and get_metric logic.

setup() {
    # Replicate get_metric from failover-watchdog.sh (line 44):
    # get_metric() { ip route | awk "/default.*$iface/{...}" | head -1 }
    # We test it inline using mock route output.

    # Replicate _uplink_label from failover-watchdog.sh (lines 83-89)
    uplink_label() {
        case "$1" in
            enx*) printf "iPhone USB" ;;
            rndis0|usb0) printf "Android USB" ;;
            bnep0) printf "Bluetooth PAN" ;;
            wlan0) printf "WiFi STA" ;;
            *) printf "%s" "$1" ;;
        esac
    }

    # get_metric mock: given route table text and interface, extract metric
    get_metric_from() {
        local routes="$1" iface="$2"
        printf '%s' "$routes" \
            | awk "/default.*$iface/{for(i=1;i<=NF;i++){if(\$i==\"metric\"){print \$(i+1);exit}}}" \
            | head -1
    }
}

@test "uplink_label: enx prefix maps to iPhone USB" {
    [ "$(uplink_label enx001a2b3c4d5e)" = "iPhone USB" ]
}

@test "uplink_label: rndis0 maps to Android USB" {
    [ "$(uplink_label rndis0)" = "Android USB" ]
}

@test "uplink_label: usb0 maps to Android USB" {
    [ "$(uplink_label usb0)" = "Android USB" ]
}

@test "uplink_label: bnep0 maps to Bluetooth PAN" {
    [ "$(uplink_label bnep0)" = "Bluetooth PAN" ]
}

@test "uplink_label: wlan0 maps to WiFi STA" {
    [ "$(uplink_label wlan0)" = "WiFi STA" ]
}

@test "get_metric: extracts metric from route table correctly" {
    local routes="default via 192.168.1.1 dev wlan0 proto dhcp metric 600"
    result=$(get_metric_from "$routes" "wlan0")
    [ "$result" = "600" ]
}

@test "get_metric: returns empty string when interface has no default route" {
    local routes="default via 192.168.1.1 dev wlan0 proto dhcp metric 600"
    result=$(get_metric_from "$routes" "enx001a2b3c4d5e")
    [ -z "$result" ]
}
