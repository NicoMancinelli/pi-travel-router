#!/bin/bash
# install/07-monitoring.sh — vnStat, Prometheus, ntfy, bandwidth dashboard, UPS
# Defines run_monitoring(). Source this file; do not execute directly.

run_monitoring() {
    # ── vnStat interface init ───────────────────────────────────────────────────
    section "vnStat"

    mkdir -p /var/lib/prometheus/node-exporter
    vnstat --add -i wlan0 2>/dev/null || true
    vnstat --add -i uap0  2>/dev/null || true
    ok "vnStat tracking wlan0 + uap0"

    # ── Prometheus node exporter ─────────────────────────────────────────────────
    section "Prometheus node exporter"

    if [[ "${ENABLE_PROMETHEUS_EXPORTER:-0}" = "1" ]]; then
        run_or_dry env DEBIAN_FRONTEND=noninteractive apt-get install -y prometheus-node-exporter 2>/dev/null || true
        run_or_dry systemctl enable --now prometheus-node-exporter 2>/dev/null || true
        ok "Prometheus node exporter enabled on :9100 (accessible via Tailscale)"
        ok "Scrape with: http://$(tailscale ip -4 2>/dev/null | head -1):9100/metrics"
    else
        ok "Prometheus node exporter disabled (set ENABLE_PROMETHEUS_EXPORTER=1 to activate)"
    fi

    # ── Bandwidth analytics dashboard ────────────────────────────────────────────
    section "Bandwidth analytics dashboard"

    local _AP_GATEWAY="${AP_GATEWAY:-10.3.141.1}"

    install_file scripts/generate-bandwidth-report.sh \
        /usr/local/bin/generate-bandwidth-report.sh 755
    install_file systemd/generate-bandwidth-report.service \
        /etc/systemd/system/generate-bandwidth-report.service 644
    install_file systemd/generate-bandwidth-report.timer \
        /etc/systemd/system/generate-bandwidth-report.timer 644
    install_file systemd/vnstat-push.service \
        /etc/systemd/system/vnstat-push.service 644
    install_file systemd/vnstat-push.timer \
        /etc/systemd/system/vnstat-push.timer 644
    install_file scripts/vnstat-push.sh /usr/local/bin/vnstat-push.sh 755
    systemctl daemon-reload

    if [[ "${ENABLE_BANDWIDTH_DASHBOARD:-0}" = "1" ]]; then
        run_or_dry systemctl enable --now generate-bandwidth-report.timer 2>/dev/null || true
        /usr/local/bin/generate-bandwidth-report.sh 2>/dev/null || true
        ln -sf /var/lib/travel-router/bandwidth.html \
            /var/www/html/bandwidth.html 2>/dev/null || true
        ok "Bandwidth dashboard: http://${_AP_GATEWAY}/bandwidth.html"
        ok "Regenerated daily at 00:05"
    else
        systemctl disable generate-bandwidth-report.timer 2>/dev/null || true
        ok "Bandwidth dashboard disabled (set ENABLE_BANDWIDTH_DASHBOARD=1)"
    fi

    # vnStat push
    if [[ -n "${PUSHGW_URL:-}" ]]; then
        run_or_dry systemctl enable --now vnstat-push.timer 2>/dev/null || true
        ok "vnStat Prometheus push enabled (hourly) → ${PUSHGW_URL}"
    else
        ok "vnStat push disabled (set PUSHGW_URL in /etc/default/travel-router)"
    fi

    # ── UPS / PiSugar battery monitor ────────────────────────────────────────────
    section "UPS battery monitor (PiSugar 3)"

    install_file scripts/ups-monitor.sh /usr/local/bin/ups-monitor.sh 755
    install_file systemd/ups-monitor.service /etc/systemd/system/ups-monitor.service 644
    install_file systemd/ups-monitor.timer   /etc/systemd/system/ups-monitor.timer   644
    systemctl daemon-reload

    if [[ "${ENABLE_UPS_MONITOR:-0}" = "1" ]]; then
        run_or_dry systemctl enable --now ups-monitor.timer 2>/dev/null || true
        ok "UPS monitor enabled — battery checked every 5 min, shutdown at ${UPS_SHUTDOWN_THRESHOLD:-10}%"
        ok "PiSugar server (optional): https://github.com/PiSugar/pisugar-power-manager-rs"
    else
        systemctl disable ups-monitor.timer 2>/dev/null || true
        ok "UPS monitor disabled (set ENABLE_UPS_MONITOR=1 to activate)"
    fi

    # ── Daily digest ─────────────────────────────────────────────────────────────
    section "Daily digest notification"

    install_file scripts/daily-digest.sh /usr/local/bin/daily-digest.sh 755

    if [[ -n "${NTFY_TOPIC:-}" ]]; then
        ok "Daily digest enabled — 08:00 ntfy push with uptime, uplink, Tailscale state"
    else
        ok "Daily digest installed — set NTFY_TOPIC in /etc/default/travel-router to activate"
    fi
}
