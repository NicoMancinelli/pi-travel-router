#!/usr/bin/env python3
"""Pi Travel Router web management dashboard — Flask REST API on :8080."""

import ast
import json
import os
import re
import subprocess
import time
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

WEB_TOKEN_FILE = "/var/lib/travel-router/web-token"
DEFAULTS_FILE = "/etc/default/travel-router"
COMBINED_LOG = "/var/log/travel-router/combined.log"
UPS_STATUS_FILE = "/var/lib/travel-router/ups-status"
WG_CONF = "/etc/wireguard/wg0.conf"

AP_SUBNETS = ("192.168.4.", "10.3.141.")
TAILSCALE_PREFIX = "100."

WHITELISTED_SERVICES = {
    "hostapd",
    "NetworkManager",
    "adguardhome",
    "wg-quick@wg0",
    "tailscaled",
    "wan-watchdog",
    "failover-watchdog",
}

# ── Auth helpers ──────────────────────────────────────────────────────────────


def _load_token():
    try:
        return Path(WEB_TOKEN_FILE).read_text().strip()
    except OSError:
        return None


def _client_ip():
    return request.remote_addr or ""


def _is_ap_client():
    ip = _client_ip()
    return any(ip.startswith(prefix) for prefix in AP_SUBNETS)


def _bearer_token():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.args.get("token", "")


def require_auth(f):
    """Decorator: skip auth for AP-subnet clients; enforce token for Tailscale."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if _is_ap_client():
            return f(*args, **kwargs)
        token = _load_token()
        if token and _bearer_token() == token:
            return f(*args, **kwargs)
        return jsonify({"error": "Unauthorized"}), 401

    return decorated


def require_auth_always(f):
    """Decorator: always require token (for write endpoints)."""

    @wraps(f)
    def decorated(*args, **kwargs):
        token = _load_token()
        if token and _bearer_token() == token:
            return f(*args, **kwargs)
        return jsonify({"error": "Unauthorized"}), 401

    return decorated


# ── Utility ───────────────────────────────────────────────────────────────────


def _run(cmd, timeout=10):
    """Run a shell command; return (stdout, returncode)."""
    try:
        result = subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        return "", 1
    except OSError:
        return "", 1


def _read_proc(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


# ── Status helpers ────────────────────────────────────────────────────────────


def _uplink_info():
    out, rc = _run("ip route show default")
    if rc != 0 or not out.strip():
        return {"name": "none", "state": "down"}
    # Parse first default route: "default via X.X.X.X dev ethN ..."
    m = re.search(r"dev\s+(\S+)", out)
    name = m.group(1) if m else "unknown"
    return {"name": name, "state": "up"}


def _ap_clients():
    out, rc = _run("iw dev uap0 station dump")
    if rc != 0:
        return []
    clients = []
    current = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Station"):
            if current:
                clients.append(current)
            parts = line.split()
            current = {"mac": parts[1] if len(parts) > 1 else "?", "signal": None, "tx_bytes": None, "rx_bytes": None}
        elif "signal:" in line:
            m = re.search(r"signal:\s*([-\d]+)", line)
            if m and current:
                current["signal"] = int(m.group(1))
        elif "tx bytes:" in line:
            m = re.search(r"tx bytes:\s*(\d+)", line)
            if m and current:
                current["tx_bytes"] = int(m.group(1))
        elif "rx bytes:" in line:
            m = re.search(r"rx bytes:\s*(\d+)", line)
            if m and current:
                current["rx_bytes"] = int(m.group(1))
    if current:
        clients.append(current)
    return clients


def _vpn_state():
    ts_out, ts_rc = _run("tailscale status --json 2>/dev/null", timeout=5)
    ts_state = "unknown"
    ts_ip = None
    if ts_rc == 0 and ts_out:
        try:
            ts_data = json.loads(ts_out)
            ts_state = ts_data.get("BackendState", "unknown")
            self_node = ts_data.get("Self", {})
            addrs = self_node.get("TailscaleIPs", [])
            ts_ip = addrs[0] if addrs else None
        except (json.JSONDecodeError, KeyError):
            pass

    wg_out, wg_rc = _run("wg show wg0 2>/dev/null", timeout=5)
    wg_state = "up" if wg_rc == 0 and wg_out.strip() else "down"

    return {
        "tailscale": {"state": ts_state, "ip": ts_ip},
        "wireguard": {"state": wg_state},
    }


def _system_stats():
    uptime_raw = _read_proc("/proc/uptime")
    uptime_secs = float(uptime_raw.split()[0]) if uptime_raw else 0
    hours, rem = divmod(int(uptime_secs), 3600)
    minutes = rem // 60
    uptime_str = f"{hours}h {minutes}m"

    loadavg_raw = _read_proc("/proc/loadavg")
    load = loadavg_raw.split()[:3] if loadavg_raw else ["?", "?", "?"]

    meminfo = _read_proc("/proc/meminfo")
    mem = {}
    for line in meminfo.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            mem[parts[0].rstrip(":")] = int(parts[1])
    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", 0)
    mem_used = mem_total - mem_avail

    return {
        "uptime": uptime_str,
        "uptime_seconds": int(uptime_secs),
        "load": load,
        "memory": {
            "total_kb": mem_total,
            "used_kb": mem_used,
            "available_kb": mem_avail,
            "percent_used": round(100 * mem_used / mem_total, 1) if mem_total else 0,
        },
    }


def _battery_info():
    try:
        raw = Path(UPS_STATUS_FILE).read_text().strip()
        # Expected format: "battery=<n>%" or "level=<n>" — be lenient
        m = re.search(r"(\d+)", raw)
        level = int(m.group(1)) if m else None
        return {"level": level, "raw": raw}
    except OSError:
        return None


# ── API routes ────────────────────────────────────────────────────────────────


@app.route("/api/status")
@require_auth
def api_status():
    return jsonify(
        {
            "uplink": _uplink_info(),
            "ap_clients": _ap_clients(),
            "vpn": _vpn_state(),
            "system": _system_stats(),
            "battery": _battery_info(),
            "timestamp": int(time.time()),
        }
    )


@app.route("/api/logs")
@require_auth
def api_logs():
    service = request.args.get("service", "")
    try:
        lines_n = max(1, min(int(request.args.get("lines", "50")), 500))
    except ValueError:
        return jsonify({"error": "Invalid lines parameter"}), 400

    try:
        all_lines = Path(COMBINED_LOG).read_text(errors="replace").splitlines()
    except OSError:
        all_lines = []

    if service:
        # Sanitise service name before using in regex
        safe = re.escape(service)
        all_lines = [l for l in all_lines if re.search(safe, l, re.IGNORECASE)]

    return jsonify({"lines": all_lines[-lines_n:], "total": len(all_lines)})


@app.route("/api/bandwidth")
@require_auth
def api_bandwidth():
    out, rc = _run("vnstat --json", timeout=15)
    if rc != 0 or not out.strip():
        return jsonify({"error": "vnstat not available or no data"}), 503
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return jsonify({"error": "Failed to parse vnstat output"}), 503
    return jsonify(data)


@app.route("/api/config", methods=["GET", "POST"])
@require_auth
def api_config():
    if request.method == "GET":
        return _config_get()
    return _config_post()


def _allowed_keys():
    """Return the set of keys that already exist in DEFAULTS_FILE."""
    keys = set()
    try:
        for line in Path(DEFAULTS_FILE).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=', line)
                if m:
                    keys.add(m.group(1))
    except OSError:
        pass
    return keys


def _config_get():
    result = {}
    allowed = _allowed_keys()
    try:
        for line in Path(DEFAULTS_FILE).read_text().splitlines():
            line_s = line.strip()
            if line_s and not line_s.startswith("#"):
                m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)', line_s)
                if m and m.group(1) in allowed:
                    val = m.group(2).strip('"').strip("'")
                    result[m.group(1)] = val
    except OSError:
        pass
    return jsonify(result)


def _config_post():
    allowed = _allowed_keys()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    # Validate keys
    bad_keys = set(data.keys()) - allowed
    if bad_keys:
        return jsonify({"error": f"Unknown or disallowed keys: {sorted(bad_keys)}"}), 400

    # Validate values: printable ASCII, no newlines/semicolons
    for k, v in data.items():
        v_str = str(v)
        if re.search(r'[\r\n;`$]', v_str):
            return jsonify({"error": f"Invalid characters in value for key: {k}"}), 400
        if len(v_str) > 512:
            return jsonify({"error": f"Value too long for key: {k}"}), 400

    try:
        content = Path(DEFAULTS_FILE).read_text()
    except OSError:
        return jsonify({"error": "Cannot read config file"}), 503

    for key, val in data.items():
        val_str = str(val)
        # Replace existing key=value line
        pattern = re.compile(
            r'^(' + re.escape(key) + r'=).*$', re.MULTILINE
        )
        replacement = f'{key}="{val_str}"'
        if pattern.search(content):
            content = pattern.sub(replacement, content)
        else:
            content += f'\n{replacement}\n'

    try:
        Path(DEFAULTS_FILE).write_text(content)
    except OSError as e:
        return jsonify({"error": f"Write failed: {e}"}), 503

    return jsonify({"ok": True, "updated": list(data.keys())})


@app.route("/api/service/<name>/restart", methods=["POST"])
@require_auth_always
def api_service_restart(name):
    if name not in WHITELISTED_SERVICES:
        return jsonify({"error": f"Service not whitelisted: {name}"}), 400
    out, rc = _run(["systemctl", "restart", name], timeout=30)
    if rc != 0:
        return jsonify({"error": f"systemctl restart failed (rc={rc})"}), 503
    return jsonify({"ok": True, "service": name})


@app.route("/api/system/reboot", methods=["POST"])
@require_auth_always
def api_system_reboot():
    _run(["systemctl", "reboot"], timeout=5)
    return jsonify({"ok": True, "message": "Reboot initiated"})


@app.route("/api/vpn/wireguard/peer", methods=["POST"])
@require_auth_always
def api_wg_add_peer():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object with peer config"}), 400

    public_key = data.get("public_key", "")
    allowed_ips = data.get("allowed_ips", "")
    endpoint = data.get("endpoint", "")
    preshared_key = data.get("preshared_key", "")

    # Validate public_key: base64url 44 chars
    if not re.match(r'^[A-Za-z0-9+/]{43}=$', public_key):
        return jsonify({"error": "Invalid public_key format"}), 400

    # Validate allowed_ips: comma-separated CIDR notation
    for cidr in allowed_ips.split(","):
        cidr = cidr.strip()
        if not re.match(r'^[\d.:a-fA-F]+/\d+$', cidr):
            return jsonify({"error": f"Invalid CIDR in allowed_ips: {cidr}"}), 400

    # Validate optional endpoint: host:port
    if endpoint and not re.match(r'^[\w.:-]+:\d+$', endpoint):
        return jsonify({"error": "Invalid endpoint format"}), 400

    # Validate optional preshared_key
    if preshared_key and not re.match(r'^[A-Za-z0-9+/]{43}=$', preshared_key):
        return jsonify({"error": "Invalid preshared_key format"}), 400

    # Build peer block
    peer_block = f"\n[Peer]\nPublicKey = {public_key}\nAllowedIPs = {allowed_ips}\n"
    if endpoint:
        peer_block += f"Endpoint = {endpoint}\n"
    if preshared_key:
        peer_block += f"PresharedKey = {preshared_key}\n"

    # Append to wg0.conf
    try:
        with open(WG_CONF, "a") as fh:
            fh.write(peer_block)
    except OSError as e:
        return jsonify({"error": f"Cannot write {WG_CONF}: {e}"}), 503

    # Apply live via wg addconf
    wg_stdin_cmd = f"echo '{peer_block}' | wg addconf wg0 /dev/stdin"
    _run(wg_stdin_cmd, timeout=10)

    return jsonify({"ok": True, "public_key": public_key})


# ── Serve index.html ──────────────────────────────────────────────────────────


@app.route("/")
@require_auth
def index():
    static_dir = Path(__file__).parent / "static"
    index_path = static_dir / "index.html"
    try:
        content = index_path.read_text()
        from flask import Response
        return Response(content, mimetype="text/html")
    except OSError:
        return jsonify({"error": "index.html not found"}), 404


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=False)
