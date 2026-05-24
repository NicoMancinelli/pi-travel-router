#!/bin/bash
# scripts/speedtest.sh — multi-method speed test, outputs JSON
# Methods tried in order:
#   1. speedtest-cli (Python)
#   2. speedtest (Ookla official CLI)
#   3. Custom curl / Cloudflare
# Output: {"download_mbps": X, "upload_mbps": X, "ping_ms": X, "server": "...", "method": "..."}
# Exit 0 on success, 1 on failure.
set -euo pipefail

# ── bc-free arithmetic (awk) ──────────────────────────────────────────────────
bytes_to_mbps() {
    # $1 = bytes/sec (float), output Mbps rounded to 2 decimal places
    awk -v b="$1" 'BEGIN { printf "%.2f\n", b * 8 / 1000000 }'
}

awk_round2() {
    awk -v v="$1" 'BEGIN { printf "%.2f\n", v }'
}

ms_from_sec() {
    # $1 = seconds (float), output milliseconds rounded to 1 decimal place
    awk -v s="$1" 'BEGIN { printf "%.1f\n", s * 1000 }'
}

# ── Method 1: speedtest-cli (Python) ─────────────────────────────────────────
if command -v speedtest-cli >/dev/null 2>&1; then
    _out="$(timeout 60 speedtest-cli --json 2>/dev/null)" || _out=""
    if [ -n "$_out" ]; then
        _dl_bps="$(printf '%s' "$_out" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["download"])' 2>/dev/null)" || _dl_bps=""
        _ul_bps="$(printf '%s' "$_out" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["upload"])' 2>/dev/null)" || _ul_bps=""
        _ping="$(printf '%s' "$_out" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["ping"])' 2>/dev/null)" || _ping=""
        _srv="$(printf '%s' "$_out" | python3 -c 'import sys,json; d=json.load(sys.stdin); s=d.get("server",{}); print(s.get("sponsor","") + " " + s.get("name",""))' 2>/dev/null)" || _srv="speedtest-cli"
        if [ -n "$_dl_bps" ] && [ -n "$_ul_bps" ] && [ -n "$_ping" ]; then
            _dl_mbps="$(bytes_to_mbps "$_dl_bps")"
            _ul_mbps="$(bytes_to_mbps "$_ul_bps")"
            _ping_ms="$(awk_round2 "$_ping")"
            printf '{"download_mbps": %s, "upload_mbps": %s, "ping_ms": %s, "server": "%s", "method": "speedtest-cli"}\n' \
                "$_dl_mbps" "$_ul_mbps" "$_ping_ms" "$_srv"
            exit 0
        fi
    fi
fi

# ── Method 2: speedtest (Ookla official CLI) ──────────────────────────────────
if command -v speedtest >/dev/null 2>&1; then
    _out="$(timeout 60 speedtest --format=json --accept-license --accept-gdpr 2>/dev/null)" || _out=""
    if [ -n "$_out" ]; then
        _dl_bps="$(printf '%s' "$_out" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["download"]["bandwidth"])' 2>/dev/null)" || _dl_bps=""
        _ul_bps="$(printf '%s' "$_out" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["upload"]["bandwidth"])' 2>/dev/null)" || _ul_bps=""
        _ping="$(printf '%s' "$_out" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["ping"]["latency"])' 2>/dev/null)" || _ping=""
        _srv="$(printf '%s' "$_out" | python3 -c 'import sys,json; d=json.load(sys.stdin); s=d.get("server",{}); print(s.get("name","") + " " + s.get("location",""))' 2>/dev/null)" || _srv="Ookla"
        if [ -n "$_dl_bps" ] && [ -n "$_ul_bps" ] && [ -n "$_ping" ]; then
            _dl_mbps="$(bytes_to_mbps "$_dl_bps")"
            _ul_mbps="$(bytes_to_mbps "$_ul_bps")"
            _ping_ms="$(awk_round2 "$_ping")"
            printf '{"download_mbps": %s, "upload_mbps": %s, "ping_ms": %s, "server": "%s", "method": "ookla"}\n' \
                "$_dl_mbps" "$_ul_mbps" "$_ping_ms" "$_srv"
            exit 0
        fi
    fi
fi

# ── Method 3: curl / Cloudflare fallback ─────────────────────────────────────

# Ping: time_connect to 1.1.1.1
_ping_sec="$(curl -o /dev/null -s -w '%{time_connect}' --max-time 10 https://1.1.1.1 2>/dev/null)" || _ping_sec="0"
_ping_ms="$(ms_from_sec "$_ping_sec")"

# Download: 25 MB test file from Cloudflare
_dl_speed="$(curl -o /dev/null -s -w '%{speed_download}' \
    --max-time 60 \
    'https://speed.cloudflare.com/__down?bytes=25000000' 2>/dev/null)" || _dl_speed="0"

if [ -z "$_dl_speed" ] || [ "$_dl_speed" = "0" ]; then
    echo '{"error": "all speed test methods failed"}' >&2
    exit 1
fi

_dl_mbps="$(bytes_to_mbps "$_dl_speed")"

# Upload: POST 5 MB of data (use /dev/urandom via dd to avoid pipe issues)
_tmp_upload="$(mktemp)"
dd if=/dev/urandom bs=1048576 count=5 2>/dev/null > "$_tmp_upload"
_ul_speed="$(curl -o /dev/null -s -w '%{speed_upload}' \
    --max-time 30 \
    -X POST \
    -T "$_tmp_upload" \
    'https://speed.cloudflare.com/__up' 2>/dev/null)" || _ul_speed="0"
rm -f "$_tmp_upload"

_ul_mbps="$(bytes_to_mbps "${_ul_speed:-0}")"

printf '{"download_mbps": %s, "upload_mbps": %s, "ping_ms": %s, "server": "Cloudflare (curl)", "method": "curl-cloudflare"}\n' \
    "$_dl_mbps" "$_ul_mbps" "$_ping_ms"
exit 0
