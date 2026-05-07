#!/bin/bash
# Disconnect Bluetooth PAN tether
dhclient -r bnep0 2>/dev/null || true
ip link set bnep0 down 2>/dev/null || true
bt-pan --dbus disconnect 2>/dev/null || true
logger -t bt-tether "Bluetooth PAN disconnected"
/usr/local/bin/notify-router.sh "Bluetooth tether disconnected" 2>/dev/null || true
systemd-run --no-block --unit="failover-watchdog-$$" /usr/local/bin/failover-watchdog.sh 2>/dev/null || \
    /usr/local/bin/failover-watchdog.sh 2>/dev/null || true
