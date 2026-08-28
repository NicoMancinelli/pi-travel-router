#!/bin/bash
# /usr/local/bin/hardware-watchdog.sh — Hardware Health, Power & Thermal Watchdog
#
# Monitors Raspberry Pi power supply voltage, throttling state, and CPU temperatures.
# Sends ntfy.sh alerts via notify-router.sh upon under-voltage or thermal events.
#
set -euo pipefail

STATE_DIR="/var/lib/travel-router"
if ! mkdir -p "${STATE_DIR}" 2>/dev/null; then
    STATE_DIR="/tmp/travel-router"
    mkdir -p "${STATE_DIR}" 2>/dev/null || true
fi
STATE_FILE="${STATE_DIR}/hardware-state.json"

LOCK_FILE="/run/lock/hardware-watchdog.lock"
if ! touch "$LOCK_FILE" 2>/dev/null; then
    LOCK_FILE="/tmp/hardware-watchdog.lock"
fi
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

NTFY_TOPIC="${NTFY_TOPIC:-}"
COOLDOWN_SEC=900  # 15 minute cooldown between repeated warnings

_notify() {
    local msg="$1" priority="${2:-normal}"
    if [[ -n "$NTFY_TOPIC" && -x /usr/local/bin/notify-router.sh ]]; then
        /usr/local/bin/notify-router.sh "$msg" "$priority" 2>/dev/null || true
    else
        logger -t hardware-watchdog "$msg"
    fi
}

now=$(date +%s)

# ── 1. Read CPU Temperature ───────────────────────────────────────────────────
temp_c=0
if [[ -f /sys/class/thermal/thermal_zone0/temp ]]; then
    raw_temp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo "0")
    if [[ "$raw_temp" =~ ^[0-9]+$ ]]; then
        temp_c=$(( raw_temp / 1000 ))
    fi
fi

# Fallback to vcgencmd measure_temp if sysfs was empty
if [[ "$temp_c" -eq 0 ]] && command -v vcgencmd >/dev/null 2>&1; then
    vc_temp=$(vcgencmd measure_temp 2>/dev/null | grep -oE '[0-9]+(\.[0-9]+)?' | cut -d. -f1 || echo "0")
    if [[ "$vc_temp" =~ ^[0-9]+$ ]]; then
        temp_c=$vc_temp
    fi
fi

# ── 2. Read Throttled Bitmask ─────────────────────────────────────────────────
throttled_hex="0x0"
throttled_val=0
vc_available=false

if command -v vcgencmd >/dev/null 2>&1; then
    raw_throttled=$(vcgencmd get_throttled 2>/dev/null || echo "")
    if [[ "$raw_throttled" =~ throttled=(0x[0-9a-fA-F]+) ]]; then
        throttled_hex="${BASH_REMATCH[1]}"
        throttled_val=$(( throttled_hex ))
        vc_available=true
    fi
fi

# Bitmask evaluation
curr_undervolt=$(( (throttled_val & 0x1) != 0 ? 1 : 0 ))
curr_freq_capped=$(( (throttled_val & 0x2) != 0 ? 1 : 0 ))
curr_throttled=$(( (throttled_val & 0x4) != 0 ? 1 : 0 ))
curr_soft_temp_limit=$(( (throttled_val & 0x8) != 0 ? 1 : 0 ))

hist_undervolt=$(( (throttled_val & 0x10000) != 0 ? 1 : 0 ))
hist_freq_capped=$(( (throttled_val & 0x20000) != 0 ? 1 : 0 ))
hist_throttled=$(( (throttled_val & 0x40000) != 0 ? 1 : 0 ))
hist_soft_temp_limit=$(( (throttled_val & 0x80000) != 0 ? 1 : 0 ))

# ── 3. Load Prior State & Cooldowns ───────────────────────────────────────────
last_undervolt_alert=0
last_thermal_alert=0

if [[ -f "$STATE_FILE" ]]; then
    last_undervolt_alert=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f).get('last_undervolt_alert', 0))
except Exception:
    print(0)
" "$STATE_FILE" 2>/dev/null || echo "0")
    last_thermal_alert=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f).get('last_thermal_alert', 0))
except Exception:
    print(0)
" "$STATE_FILE" 2>/dev/null || echo "0")
fi

# ── 4. Alerting Checks ────────────────────────────────────────────────────────
new_undervolt_alert=$last_undervolt_alert
new_thermal_alert=$last_thermal_alert

# Under-voltage alert
if [[ $curr_undervolt -eq 1 ]]; then
    if (( now - last_undervolt_alert >= COOLDOWN_SEC )); then
        _notify "CRITICAL: Power supply under-voltage (<4.63V) detected! Check power adapter/cable." urgent
        new_undervolt_alert=$now
    fi
fi

# Thermal throttling / Critical Temp alert
if [[ $curr_throttled -eq 1 || $temp_c -ge 80 ]]; then
    if (( now - last_thermal_alert >= COOLDOWN_SEC )); then
        _notify "CRITICAL: CPU thermal throttling active (${temp_c}°C)! Ensure proper airflow." urgent
        new_thermal_alert=$now
    fi
elif [[ $temp_c -ge 72 ]]; then
    if (( now - last_thermal_alert >= COOLDOWN_SEC )); then
        _notify "WARNING: High CPU temperature (${temp_c}°C)." high
        new_thermal_alert=$now
    fi
fi

# ── 5. Save Current State Atomically ──────────────────────────────────────────
tmp_file="${STATE_FILE}.tmp.$$"
python3 -c "
import json, sys
data = {
    'timestamp': int(sys.argv[1]),
    'cpu_temp_c': int(sys.argv[2]),
    'vcgencmd_available': sys.argv[3].lower() == 'true',
    'throttled_hex': sys.argv[4],
    'currently_undervolted': int(sys.argv[5]) == 1,
    'currently_freq_capped': int(sys.argv[6]) == 1,
    'currently_throttled': int(sys.argv[7]) == 1,
    'soft_temp_limit': int(sys.argv[8]) == 1,
    'undervoltage_occurred': int(sys.argv[9]) == 1,
    'freq_capping_occurred': int(sys.argv[10]) == 1,
    'throttling_occurred': int(sys.argv[11]) == 1,
    'soft_temp_limit_occurred': int(sys.argv[12]) == 1,
    'last_undervolt_alert': int(sys.argv[13]),
    'last_thermal_alert': int(sys.argv[14])
}
with open(sys.argv[15], 'w') as f:
    json.dump(data, f, indent=2)
" "$now" "$temp_c" "$vc_available" "$throttled_hex" \
  "$curr_undervolt" "$curr_freq_capped" "$curr_throttled" "$curr_soft_temp_limit" \
  "$hist_undervolt" "$hist_freq_capped" "$hist_throttled" "$hist_soft_temp_limit" \
  "$new_undervolt_alert" "$new_thermal_alert" "$tmp_file"

chmod 0644 "$tmp_file" 2>/dev/null || true
mv "$tmp_file" "$STATE_FILE"
