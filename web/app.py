#!/usr/bin/env python3
"""Pi Travel Router web management dashboard — Flask REST API on :8080."""

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

WEB_TOKEN_FILE = "/var/lib/travel-router/web-token"
DEFAULTS_FILE = "/etc/default/travel-router"
COMBINED_LOG = "/var/log/travel-router/combined.log"
UPS_STATUS_FILE = "/var/lib/travel-router/ups-status"
WG_CONF = "/etc/wireguard/wg0.conf"
ACTIVE_PROFILE_FILE = "/var/lib/travel-router/active-profile"
APPLY_PROFILE_SCRIPT = "/usr/local/sbin/apply-privacy-profile.sh"
VALID_PROFILES = {"vpn-only", "adblock-only", "tor", "direct"}

AP_SUBNETS = ("192.168.4.", "10.3.141.")

# ── Status cache ──────────────────────────────────────────────────────────────
_STATUS_CACHE: dict = {"ts": 0.0, "data": {}}
_STATUS_CACHE_TTL = 5  # seconds
TAILSCALE_PREFIX = "100."

# ── Config value validators ───────────────────────────────────────────────────
import re as _re

_CONFIG_VALIDATORS = {
    # ENABLE_* keys must be 0 or 1
    _re.compile(r'^ENABLE_'): lambda v: v in ('0', '1') or "must be 0 or 1",
    # Port keys must be integers 1024-65535
    _re.compile(r'_(PORT|LISTEN_PORT)$'): lambda v: (v.isdigit() and 1024 <= int(v) <= 65535) or "must be a port number 1024-65535",
    # IP/target keys must not contain shell metacharacters
    _re.compile(r'(TARGET|ADDR|SERVER)$'): lambda v: not _re.search(r'[;&|`$]', v) or "invalid characters in value",
}


def _validate_config_value(key, value):
    for pattern, validator in _CONFIG_VALIDATORS.items():
        if pattern.search(key):
            result = validator(value)
            if result is not True:
                return result
    return None  # No validator matched → allow


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


def _wg_handshake_ago(seconds_ago):
    """Convert seconds-since-handshake to human-readable string."""
    if seconds_ago is None:
        return "never"
    s = int(seconds_ago)
    if s < 5:
        return "just now"
    if s < 60:
        return f"{s} seconds ago"
    if s < 3600:
        return f"{s // 60} minutes ago"
    if s < 86400:
        return f"{s // 3600} hours ago"
    return f"{s // 86400} days ago"


def _parse_wg_peers(wg_out):
    """Parse `wg show wg0` output into a list of peer dicts with extended info."""
    peers = []
    current = {}
    now = int(time.time())

    for line in wg_out.splitlines():
        line = line.strip()
        if line.startswith("peer:"):
            if current.get("public_key"):
                peers.append(current)
            current = {
                "public_key": line.split(":", 1)[1].strip(),
                "endpoint": None,
                "allowed_ips": None,
                "last_handshake_ago": "never",
                "rx_bytes": None,
                "tx_bytes": None,
            }
        elif line.startswith("endpoint:") and current:
            current["endpoint"] = line.split(":", 1)[1].strip()
        elif line.startswith("allowed ips:") and current:
            current["allowed_ips"] = line.split(":", 1)[1].strip()
        elif line.startswith("latest handshake:") and current:
            hs_str = line.split(":", 1)[1].strip()
            # wg show formats as e.g. "1 minute, 23 seconds ago" or a Unix timestamp
            # wg show wg0 dump gives seconds; wg show gives human text
            # Try to parse seconds from patterns like "X seconds ago", "X minutes ago", etc.
            total_secs = None
            m = re.match(
                r"(?:(\d+)\s+days?,\s*)?(?:(\d+)\s+hours?,\s*)?(?:(\d+)\s+minutes?,\s*)?(?:(\d+)\s+seconds?)?",
                hs_str,
            )
            if m and any(m.groups()):
                d = int(m.group(1) or 0)
                h = int(m.group(2) or 0)
                mn = int(m.group(3) or 0)
                s = int(m.group(4) or 0)
                total_secs = d * 86400 + h * 3600 + mn * 60 + s
            elif re.match(r"^\d+$", hs_str):
                # raw unix timestamp from wg show dump
                total_secs = now - int(hs_str)
            current["last_handshake_ago"] = _wg_handshake_ago(total_secs)
        elif line.startswith("transfer:") and current:
            # "transfer: 1.23 MiB received, 4.56 MiB sent"
            m = re.search(r"([\d.]+)\s*(\w+)\s+received,\s*([\d.]+)\s*(\w+)\s+sent", line)
            if m:
                def _to_bytes(val, unit):
                    val = float(val)
                    unit = unit.lower()
                    if unit in ("kib", "kb"):
                        return int(val * 1024)
                    if unit in ("mib", "mb"):
                        return int(val * 1024 * 1024)
                    if unit in ("gib", "gb"):
                        return int(val * 1024 * 1024 * 1024)
                    return int(val)
                current["rx_bytes"] = _to_bytes(m.group(1), m.group(2))
                current["tx_bytes"] = _to_bytes(m.group(3), m.group(4))

    if current.get("public_key"):
        peers.append(current)
    return peers


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
    wg_peers = _parse_wg_peers(wg_out) if wg_rc == 0 else []

    return {
        "tailscale": {"state": ts_state, "ip": ts_ip},
        "wireguard": {"state": wg_state, "peers": wg_peers},
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
        "cpu_temp_c": _cpu_temp(),
        "disk": _disk_usage(),
    }


def _cpu_temp():
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return round(int(raw) / 1000, 1)  # millidegrees → °C
    except Exception:
        return None


def _disk_usage():
    try:
        u = shutil.disk_usage("/")
        return {
            "total_gb": round(u.total / 1e9, 1),
            "used_gb": round(u.used / 1e9, 1),
            "percent": round(u.used / u.total * 100, 1),
        }
    except Exception:
        return None


def _uptime_seconds():
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:
        return None


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
    now = time.monotonic()
    if now - _STATUS_CACHE["ts"] < _STATUS_CACHE_TTL and _STATUS_CACHE["data"]:
        return jsonify(_STATUS_CACHE["data"])

    result_dict = {
        "uplink": _uplink_info(),
        "ap_clients": _ap_clients(),
        "vpn": _vpn_state(),
        "system": _system_stats(),
        "battery": _battery_info(),
        "timestamp": int(time.time()),
    }
    _STATUS_CACHE["ts"] = time.monotonic()
    _STATUS_CACHE["data"] = result_dict
    return jsonify(result_dict)


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

    # Semantic validation by key pattern
    for key, value in data.items():
        err = _validate_config_value(key, str(value))
        if err:
            return jsonify({"error": f"Invalid value for {key}: {err}"}), 400

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
    import threading
    delay = 30

    def _do_reboot():
        import time as _t
        _t.sleep(delay)
        subprocess.run(["systemctl", "reboot"], timeout=10)

    threading.Thread(target=_do_reboot, daemon=True).start()
    return jsonify({"rebooting": True, "in_seconds": delay,
                    "message": f"Reboot scheduled in {delay} seconds"})


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

    # Verify the peer was actually written (re-read and check)
    try:
        conf_content = Path(WG_CONF).read_text()
        if public_key not in conf_content:
            return jsonify({"error": "Write verification failed — peer not found in conf after write"}), 500
    except Exception as e:
        return jsonify({"error": f"Verification read failed: {e}"}), 500

    # Try to activate live if wg0 interface is already up
    try:
        result = subprocess.run(["ip", "link", "show", "wg0"], capture_output=True, timeout=3)
        if result.returncode == 0:
            subprocess.run(["wg", "addconf", "wg0", WG_CONF], capture_output=True, timeout=5)
    except Exception:
        pass  # Interface may not be up yet; peer will load on next wg-quick start

    return jsonify({"ok": True, "public_key": public_key})


# ── WireGuard peer helpers ─────────────────────────────────────────────────────


def _read_wg_conf():
    """Read wg0.conf; return list of lines. Raises OSError on failure."""
    return Path(WG_CONF).read_text().splitlines(keepends=True)


def _find_peer_block(lines, pubkey):
    """Return (start, end) line indices (inclusive) for the [Peer] block matching
    pubkey, or (None, None) if not found.  end includes any trailing blank line."""
    start = None
    in_block = False
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[Peer]":
            if in_block and not found:
                # previous peer block didn't match — reset
                pass
            in_block = True
            start = i
            found = False
        elif stripped.startswith("[") and stripped.endswith("]") and stripped != "[Peer]":
            if in_block and found:
                # end of matching block (next section started)
                end = i - 1
                # include trailing blank line before next section
                while end > start and not lines[end].strip():
                    end -= 1
                return start, end
            in_block = False
            found = False
            start = None
        elif in_block:
            m = re.match(r"^PublicKey\s*=\s*(.+)$", stripped)
            if m and m.group(1).strip() == pubkey:
                found = True

    # EOF: flush last block
    if in_block and found and start is not None:
        end = len(lines) - 1
        while end > start and not lines[end].strip():
            end -= 1
        return start, end

    return None, None


def _remove_peer_from_conf(pubkey):
    """Remove the [Peer] block for pubkey from wg0.conf atomically.
    Returns (True, None) on success, (False, error_str) on failure."""
    try:
        lines = _read_wg_conf()
    except OSError as exc:
        return False, f"Cannot read {WG_CONF}: {exc}"

    start, end = _find_peer_block(lines, pubkey)
    if start is None:
        return False, f"Peer not found in {WG_CONF}"

    # Remove the block (including one following blank line if present)
    remove_end = end
    if remove_end + 1 < len(lines) and not lines[remove_end + 1].strip():
        remove_end += 1

    new_lines = lines[:start] + lines[remove_end + 1:]

    try:
        conf_dir = str(Path(WG_CONF).parent)
        fd, tmp = tempfile.mkstemp(dir=conf_dir, prefix="wg0.conf.")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.writelines(new_lines)
            os.chmod(tmp, 0o600)
            os.replace(tmp, WG_CONF)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        return False, f"Cannot write {WG_CONF}: {exc}"

    return True, None


@app.route("/api/vpn/wireguard/peer/<path:pubkey_encoded>", methods=["DELETE"])
@require_auth_always
def api_wg_delete_peer(pubkey_encoded):
    pubkey = urllib.parse.unquote(pubkey_encoded).strip()

    if not re.match(r"^[A-Za-z0-9+/]{43}=$", pubkey):
        return jsonify({"error": "Invalid public_key format"}), 400

    ok, err = _remove_peer_from_conf(pubkey)
    if not ok:
        status = 404 if "not found" in (err or "").lower() else 503
        return jsonify({"error": err}), status

    # Remove live if wg0 is up
    try:
        result = subprocess.run(
            ["ip", "link", "show", "wg0"], capture_output=True, timeout=3
        )
        if result.returncode == 0:
            subprocess.run(
                ["wg", "set", "wg0", "peer", pubkey, "remove"],
                capture_output=True,
                timeout=5,
            )
    except Exception:
        pass  # best-effort; conf is already updated

    return jsonify({"ok": True})


@app.route("/api/vpn/wireguard/peer/<path:pubkey_encoded>/qr")
@require_auth
def api_wg_peer_qr(pubkey_encoded):
    pubkey = urllib.parse.unquote(pubkey_encoded).strip()

    if not re.match(r"^[A-Za-z0-9+/]{43}=$", pubkey):
        return jsonify({"error": "Invalid public_key format"}), 400

    # Read server public key
    server_pubkey = ""
    try:
        server_pubkey = Path("/etc/wireguard/public.key").read_text().strip()
    except OSError:
        pass
    if not server_pubkey:
        try:
            privkey_raw = ""
            for line in Path(WG_CONF).read_text().splitlines():
                m = re.match(r"^\s*PrivateKey\s*=\s*(.+)$", line)
                if m:
                    privkey_raw = m.group(1).strip()
                    break
            if privkey_raw:
                result = subprocess.run(
                    ["wg", "pubkey"],
                    input=privkey_raw,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    server_pubkey = result.stdout.strip()
        except Exception:
            pass

    if not server_pubkey:
        return jsonify({"error": "Cannot determine server public key"}), 503

    # Read WG_ENDPOINT from /etc/default/travel-router
    endpoint = ""
    try:
        for line in Path(DEFAULTS_FILE).read_text().splitlines():
            m = re.match(r"^WG_ENDPOINT\s*=\s*[\"']?([^\"'\s]+)[\"']?", line)
            if m:
                endpoint = m.group(1).strip()
                break
    except OSError:
        pass

    # Fall back to detecting server IP
    if not endpoint:
        try:
            out, rc = _run("curl -sf --max-time 3 https://api.ipify.org", timeout=5)
            if rc == 0 and out.strip():
                endpoint = out.strip()
        except Exception:
            pass
    if not endpoint:
        endpoint = "YOUR_SERVER_IP"

    # Determine listen port
    listen_port = "51820"
    try:
        for line in Path(WG_CONF).read_text().splitlines():
            m = re.match(r"^\s*ListenPort\s*=\s*(\d+)$", line)
            if m:
                listen_port = m.group(1)
                break
    except OSError:
        pass

    # Find peer's AllowedIPs from wg0.conf
    peer_allowed_ips = ""
    try:
        lines = _read_wg_conf()
        start, end = _find_peer_block(lines, pubkey)
        if start is not None:
            for line in lines[start:end + 1]:
                m = re.match(r"^\s*AllowedIPs\s*=\s*(.+)$", line.strip())
                if m:
                    peer_allowed_ips = m.group(1).strip()
                    break
    except OSError:
        pass

    if not peer_allowed_ips:
        peer_allowed_ips = "10.0.0.X/32"  # placeholder if not found

    client_conf = (
        "[Interface]\n"
        "PrivateKey = REPLACE_WITH_CLIENT_PRIVATE_KEY\n"
        f"Address = {peer_allowed_ips}\n"
        "DNS = 1.1.1.1\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {server_pubkey}\n"
        f"Endpoint = {endpoint}:{listen_port}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        "PersistentKeepalive = 25\n"
    )

    try:
        result = subprocess.run(
            ["qrencode", "-t", "PNG", "-o", "-"],
            input=client_conf,
            capture_output=True,
            text=False,
            timeout=10,
        )
    except FileNotFoundError:
        return jsonify({"error": "qrencode not installed"}), 503
    except Exception as exc:
        return jsonify({"error": f"qrencode failed: {exc}"}), 503

    if result.returncode != 0:
        return jsonify({"error": "qrencode failed"}), 503

    return Response(result.stdout, mimetype="image/png")


# ── Serve index.html ──────────────────────────────────────────────────────────


@app.route("/")
@require_auth
def index():
    static_dir = Path(__file__).parent / "static"
    index_path = static_dir / "index.html"
    try:
        content = index_path.read_text()
        return Response(content, mimetype="text/html")
    except OSError:
        return jsonify({"error": "index.html not found"}), 404


# ── Entrypoint ────────────────────────────────────────────────────────────────

@app.route("/api/privacy/profile", methods=["GET"])
@require_auth
def api_privacy_profile_get():
    try:
        active = Path(ACTIVE_PROFILE_FILE).read_text().strip()
    except OSError:
        active = "vpn-only"
    return jsonify({"active": active, "profiles": sorted(VALID_PROFILES)})


@app.route("/api/privacy/profile", methods=["POST"])
@require_auth_always
def api_privacy_profile_set():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400
    profile = data.get("profile", "")
    if profile not in VALID_PROFILES:
        return jsonify({"error": f"Invalid profile: {profile}. Must be one of {sorted(VALID_PROFILES)}"}), 400
    try:
        result = subprocess.run(
            [APPLY_PROFILE_SCRIPT, profile],
            timeout=30,
            capture_output=True,
            text=True,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return jsonify({"error": str(exc)}), 503
    if result.returncode != 0:
        return jsonify({"error": result.stderr or result.stdout}), 503
    return jsonify({"ok": True, "profile": profile})


@app.route('/api/system/update-check', methods=['GET'])
@require_auth
def update_check():
    """Check latest available version from GitHub releases without downloading."""
    import urllib.request as _urllib_req
    try:
        url = "https://api.github.com/repos/NicoMancinelli/pi-travel-router/releases/latest"
        req = _urllib_req.Request(url, headers={"User-Agent": "pi-travel-router"})
        with _urllib_req.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        latest = data.get("tag_name", "").lstrip("v")
        current = Path("/etc/travel-router-version").read_text().strip() if Path("/etc/travel-router-version").exists() else "unknown"
        return jsonify({
            "current_version": current,
            "latest_version": latest,
            "update_available": latest != current and latest != "",
            "release_url": data.get("html_url", ""),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route('/api/system/diagnostic', methods=['POST'])
@require_auth_always
def api_diagnostic():
    """Run travel-diagnostic.sh and return its output."""
    try:
        result = subprocess.run(
            ["/usr/local/sbin/travel-diagnostic.sh"],
            capture_output=True, text=True, timeout=30
        )
        return jsonify({"output": result.stdout + result.stderr, "exit_code": result.returncode})
    except FileNotFoundError:
        return jsonify({"error": "Diagnostic script not found"}), 503
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Diagnostic timed out"}), 504


@app.route('/api/system/ota-update', methods=['POST'])
@require_auth_always
def ota_update():
    """Trigger OTA update to inactive slot (long-running, backgrounded)."""
    import threading
    url = request.json.get('url', '') if request.is_json else ''

    def _run():
        cmd = ['/usr/local/sbin/ota-update']
        if url:
            cmd.append(url)
        subprocess.run(cmd, capture_output=True, timeout=600)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started", "message": "OTA update running in background. Check logs for progress."})


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=False)
