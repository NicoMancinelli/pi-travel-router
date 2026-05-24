#!/usr/bin/env python3
"""Pi Travel Router web management dashboard — Flask REST API on :8080."""

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
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
QOS_LIMITS_FILE = "/var/lib/travel-router/qos-limits.json"
APPLY_QOS_SCRIPT = "/usr/local/sbin/apply-qos.sh"
CAPTIVE_JSON = "/var/lib/travel-router/captive-portal.json"
CAPTIVE_CREDS_FILE = "/var/lib/travel-router/captive-creds.json"
_BW_HISTORY_FILE = "/var/lib/travel-router/bw-history.json"
DOH_SCRIPT = "/usr/local/sbin/set-doh-resolver.sh"
DOH_PRESETS = ["cloudflare", "quad9", "nextdns", "adguard", "system"]
MOUNT_STORAGE_SCRIPT = "/usr/local/sbin/mount-storage.sh"
WOL_TARGETS_FILE = "/var/lib/travel-router/wol-targets.json"
DATACAP_FILE = "/var/lib/travel-router/datacap.json"
ALIASES_FILE = "/var/lib/travel-router/aliases.json"

AP_SUBNETS = ("192.168.4.", "10.3.141.")

# ── Event bus ─────────────────────────────────────────────────────────────────
_event_queue: list = []
_event_lock = threading.Lock()


def _push_event(type_: str, data: dict) -> None:
    """Append an event to the queue, keeping only the last 50."""
    event = {"type": type_, "ts": int(time.time()), **data}
    with _event_lock:
        _event_queue.append(event)
        if len(_event_queue) > 50:
            del _event_queue[:-50]


# ── Status cache ──────────────────────────────────────────────────────────────
_STATUS_CACHE: dict = {"ts": 0.0, "data": {}}

# ── Speed test cache ───────────────────────────────────────────────────────────
_SPEEDTEST_RESULT: dict = {"ts": 0.0, "result": None}
_STATUS_CACHE_TTL = 5  # seconds
TAILSCALE_PREFIX = "100."

# ── Bandwidth history sampler ─────────────────────────────────────────────────

_BW_SAMPLE_INTERVAL = 300   # 5 minutes
_BW_MAX_ENTRIES = 288       # 24 hours at 5-min intervals
_BW_UPLINK_IFACES = ("wlan0", "eth0", "usb0")
_bw_prev_sample: dict = {}   # {"iface": str, "rx": int, "tx": int}


def _read_proc_net_dev():
    """Return dict of iface -> (rx_bytes, tx_bytes) from /proc/net/dev."""
    result = {}
    try:
        text = Path("/proc/net/dev").read_text()
        for line in text.splitlines()[2:]:  # skip 2-line header
            parts = line.split(":")
            if len(parts) != 2:
                continue
            iface = parts[0].strip()
            fields = parts[1].split()
            if len(fields) >= 9:
                rx = int(fields[0])
                tx = int(fields[8])
                result[iface] = (rx, tx)
    except Exception:
        pass
    return result


def _bw_sample_once():
    """Take one bandwidth sample and append to the history file."""
    global _bw_prev_sample
    try:
        dev_stats = _read_proc_net_dev()
        # Pick the first available uplink interface
        iface = None
        for candidate in _BW_UPLINK_IFACES:
            if candidate in dev_stats:
                iface = candidate
                break
        if iface is None:
            return

        rx_now, tx_now = dev_stats[iface]
        now_ts = int(time.time())

        prev = _bw_prev_sample.get(iface)
        if prev is not None:
            # Handle 32-bit counter rollover
            rx_delta = (rx_now - prev["rx"]) % (2 ** 32)
            tx_delta = (tx_now - prev["tx"]) % (2 ** 32)

            entry = {
                "ts": now_ts,
                "rx_bytes": rx_delta,
                "tx_bytes": tx_delta,
                "iface": iface,
            }

            # Read existing history (tolerate missing / corrupt file)
            try:
                history = json.loads(Path(_BW_HISTORY_FILE).read_text())
                if not isinstance(history, list):
                    history = []
            except Exception:
                history = []

            history.append(entry)
            # Trim to max entries
            history = history[-_BW_MAX_ENTRIES:]

            # Atomic write: tmp then rename
            hist_dir = str(Path(_BW_HISTORY_FILE).parent)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=hist_dir, prefix="bw-history.")
            try:
                with os.fdopen(tmp_fd, "w") as fh:
                    json.dump(history, fh)
                os.replace(tmp_path, _BW_HISTORY_FILE)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        # Update previous sample regardless (so next interval has a baseline)
        _bw_prev_sample[iface] = {"rx": rx_now, "tx": tx_now}
    except Exception:
        pass  # Never crash the background thread


def _bw_sampler_loop():
    """Background thread: sample bandwidth every _BW_SAMPLE_INTERVAL seconds."""
    while True:
        time.sleep(_BW_SAMPLE_INTERVAL)
        _bw_sample_once()


# Start background sampler when the module loads
threading.Thread(target=_bw_sampler_loop, daemon=True).start()

# ── Latency history sampler ───────────────────────────────────────────────────

_LATENCY_HISTORY: list = []
_LATENCY_LOCK = threading.Lock()
_LATENCY_MAX_ENTRIES = 288   # 24h at 5-min intervals


def _latency_sample_once():
    """Ping the default gateway once and record RTT (or None on loss)."""
    try:
        gw_out, gw_rc = _run("ip route show default")
        if gw_rc != 0 or not gw_out.strip():
            return
        m = re.search(r"via\s+(\S+)", gw_out)
        if not m:
            return
        gateway = m.group(1)

        rtt = None
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "2", gateway],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                tm = re.search(r"time=([\d.]+)\s*ms", result.stdout)
                if tm:
                    rtt = float(tm.group(1))
            # If returncode != 0 (packet loss), rtt stays None
        except FileNotFoundError:
            # ping not available on this platform (e.g. macOS dev machine)
            pass

        entry = {"ts": int(time.time()), "rtt_ms": rtt, "gateway": gateway}
        with _LATENCY_LOCK:
            _LATENCY_HISTORY.append(entry)
            if len(_LATENCY_HISTORY) > _LATENCY_MAX_ENTRIES:
                del _LATENCY_HISTORY[:-_LATENCY_MAX_ENTRIES]
    except Exception:
        pass  # Never crash the background thread


def _latency_sampler_loop():
    """Background thread: sample latency every 300 seconds."""
    while True:
        time.sleep(300)
        try:
            _latency_sample_once()
        except Exception:
            pass


# Start background latency sampler when the module loads
threading.Thread(target=_latency_sampler_loop, daemon=True).start()

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


def _ap_clients_rich():
    """Return AP clients with hostname, IP, and connected_since from iw + dnsmasq leases."""
    out, rc = _run("iw dev uap0 station dump")
    if rc != 0:
        return []

    # Parse iw station dump
    clients = []
    current = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Station"):
            if current:
                clients.append(current)
            parts = line.split()
            current = {
                "mac": parts[1] if len(parts) > 1 else "?",
                "signal": None,
                "tx_bytes": None,
                "rx_bytes": None,
                "connected_since": None,
            }
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
        elif "connected time:" in line:
            m = re.search(r"connected time:\s*(\d+)", line)
            if m and current:
                current["connected_since"] = int(m.group(1))
    if current:
        clients.append(current)

    # Load dnsmasq leases: timestamp mac ip hostname client-id
    leases = {}  # mac -> {"ip": ..., "hostname": ...}
    try:
        for lease_line in Path("/var/lib/misc/dnsmasq.leases").read_text().splitlines():
            parts = lease_line.split()
            if len(parts) >= 4:
                mac_l = parts[1].lower()
                ip_l = parts[2]
                hostname_l = parts[3] if parts[3] != "*" else None
                leases[mac_l] = {"ip": ip_l, "hostname": hostname_l}
    except OSError:
        pass

    # Merge lease info
    for c in clients:
        lease = leases.get(c["mac"].lower(), {})
        c["ip"] = lease.get("ip")
        c["hostname"] = lease.get("hostname")

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


@app.route("/api/events/stream")
@require_auth
def api_events_stream():
    """Server-Sent Events stream for real-time connection event notifications."""

    def generate():
        deadline = time.monotonic() + 300  # 5-minute max connection
        last_seen = len(_event_queue)
        wake = threading.Event()
        try:
            while time.monotonic() < deadline:
                wake.wait(timeout=2)
                wake.clear()
                with _event_lock:
                    pending = _event_queue[last_seen:]
                    last_seen = len(_event_queue)
                if pending:
                    for evt in pending:
                        yield "data: " + json.dumps(evt) + "\n\n"
                else:
                    yield "data: {}\n\n"
        except GeneratorExit:
            pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/events")
@require_auth
def api_events():
    """Polling fallback — return last 20 events."""
    with _event_lock:
        events = list(_event_queue[-20:])
    return jsonify({"events": events})


def _parse_log_level(line):
    """Return normalised log level string for a log line, or None if undetectable."""
    # Try JSON structured log first: {"level": "info", ...}
    if line.lstrip().startswith("{"):
        try:
            obj = json.loads(line)
            lvl = str(obj.get("level", "")).lower()
            if lvl in ("debug", "info", "warn", "warning", "error", "fatal", "critical"):
                return "warn" if lvl == "warning" else ("error" if lvl in ("fatal", "critical") else lvl)
        except (json.JSONDecodeError, AttributeError):
            pass
    # Text patterns: [INFO], [WARN], [ERROR], [DEBUG] or uppercase words
    m = re.search(
        r'\[(debug|info|warn(?:ing)?|error|fatal|critical)\]',
        line, re.IGNORECASE
    )
    if m:
        lvl = m.group(1).lower()
        return "warn" if lvl in ("warn", "warning") else ("error" if lvl in ("fatal", "critical") else lvl)
    # systemd/journald style: daemon[pid]: LEVEL:
    m2 = re.search(r'\b(DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL)\b', line)
    if m2:
        lvl = m2.group(1).lower()
        return "warn" if lvl in ("warn", "warning") else ("error" if lvl in ("fatal", "critical") else lvl)
    return None


def _parse_log_timestamp(line):
    """Try to extract a datetime from a log line. Returns datetime or None."""
    # ISO 8601 / journald style: 2024-01-15T12:34:56 or 2024-01-15 12:34:56
    m = re.search(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line)
    if m:
        ts_str = m.group(1).replace(" ", "T")
        try:
            return datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # JSON log: {"ts": 1234567890, ...} or {"time": "..."}
    if line.lstrip().startswith("{"):
        try:
            obj = json.loads(line)
            for key in ("ts", "time", "timestamp", "@timestamp"):
                val = obj.get(key)
                if isinstance(val, (int, float)):
                    return datetime.fromtimestamp(val, tz=timezone.utc)
                if isinstance(val, str):
                    try:
                        return datetime.fromisoformat(val.rstrip("Z")).replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass
        except (json.JSONDecodeError, AttributeError, OSError):
            pass
    return None


@app.route("/api/logs/levels")
@require_auth
def api_logs_levels():
    return jsonify({"levels": ["debug", "info", "warn", "error"]})


@app.route("/api/logs")
@require_auth
def api_logs():
    service = request.args.get("service", "")
    level_filter = request.args.get("level", "").lower()
    since_str = request.args.get("since", "")
    q_filter = request.args.get("q", "").lower()
    try:
        limit = max(1, min(int(request.args.get("limit", request.args.get("lines", "200"))), 1000))
    except ValueError:
        return jsonify({"error": "Invalid limit parameter"}), 400

    # Parse "since" timestamp
    since_dt = None
    if since_str:
        try:
            since_dt = datetime.fromisoformat(since_str.rstrip("Z")).replace(tzinfo=timezone.utc)
        except ValueError:
            return jsonify({"error": "Invalid since parameter (expected ISO 8601)"}), 400

    try:
        all_lines = Path(COMBINED_LOG).read_text(errors="replace").splitlines()
    except OSError:
        all_lines = []

    if service:
        safe = re.escape(service)
        all_lines = [l for l in all_lines if re.search(safe, l, re.IGNORECASE)]

    if level_filter and level_filter in ("debug", "info", "warn", "error"):
        def _line_matches_level(line, wanted):
            lvl = _parse_log_level(line)
            if lvl is None:
                return True  # don't discard undetectable lines
            return lvl == wanted
        all_lines = [l for l in all_lines if _line_matches_level(l, level_filter)]

    if since_dt:
        filtered = []
        for line in all_lines:
            ts = _parse_log_timestamp(line)
            if ts is None or ts >= since_dt:
                filtered.append(line)
        all_lines = filtered

    if q_filter:
        all_lines = [l for l in all_lines if q_filter in l.lower()]

    return jsonify({"lines": all_lines[-limit:], "total": len(all_lines)})


@app.route("/api/clients")
@require_auth
def api_clients():
    clients = _ap_clients_rich()
    aliases = _read_aliases()
    for c in clients:
        mac_key = c.get("mac", "").lower().replace("-", ":").replace(" ", "")
        c["alias"] = aliases.get(mac_key, "")
    return jsonify({"clients": clients, "count": len(clients)})


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


@app.route("/api/bandwidth/history")
@require_auth
def api_bandwidth_history():
    try:
        history = json.loads(Path(_BW_HISTORY_FILE).read_text())
        if not isinstance(history, list):
            history = []
    except (OSError, json.JSONDecodeError):
        history = []
    return jsonify(history)


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
    delay = 30

    def _do_reboot():
        import time as _t
        _t.sleep(delay)
        subprocess.run(["systemctl", "reboot"], timeout=10)

    _push_event("reboot_scheduled", {"in_seconds": delay})
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

    _push_event("wg_peer_added", {"key_prefix": public_key[:8]})
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


# ── Guest network ─────────────────────────────────────────────────────────────


def _read_defaults_value(key: str, default: str = "") -> str:
    """Read a single key from the defaults file."""
    try:
        for line in Path(DEFAULTS_FILE).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
                if m and m.group(1) == key:
                    return m.group(2).strip('"').strip("'")
    except OSError:
        pass
    return default


def _guest_clients():
    """Return list of clients connected to the guest AP (uap1)."""
    out, rc = _run("iw dev uap1 station dump")
    if rc != 0:
        return []
    clients = []
    current: dict = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Station"):
            if current:
                clients.append(current)
            parts = line.split()
            current = {"mac": parts[1] if len(parts) > 1 else "?"}
        elif "signal:" in line:
            m = re.search(r"signal:\s*([-\d]+)", line)
            if m and current:
                current["signal"] = int(m.group(1))
    if current:
        clients.append(current)
    return clients


@app.route("/api/guest-network", methods=["GET"])
@require_auth
def api_guest_network_get():
    enabled_str = _read_defaults_value("ENABLE_GUEST_NETWORK", "0")
    ssid = _read_defaults_value("GUEST_SSID", "TravelRouter-Guest")
    enabled = enabled_str == "1"
    clients = _guest_clients() if enabled else []
    return jsonify({"enabled": enabled, "ssid": ssid, "clients": clients})


@app.route("/api/guest-network", methods=["POST"])
@require_auth_always
def api_guest_network_post():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    if "enabled" not in data:
        return jsonify({"error": "Missing 'enabled' field"}), 400

    new_enabled = bool(data["enabled"])
    new_val = "1" if new_enabled else "0"

    # Write to defaults file using the existing config write pattern
    try:
        content = Path(DEFAULTS_FILE).read_text()
    except OSError as e:
        return jsonify({"error": f"Cannot read config file: {e}"}), 503

    pattern = re.compile(r'^(ENABLE_GUEST_NETWORK=).*$', re.MULTILINE)
    replacement = f'ENABLE_GUEST_NETWORK="{new_val}"'
    if pattern.search(content):
        content = pattern.sub(replacement, content)
    else:
        content += f'\n{replacement}\n'

    try:
        Path(DEFAULTS_FILE).write_text(content)
    except OSError as e:
        return jsonify({"error": f"Write failed: {e}"}), 503

    # Start or stop the guest hostapd service
    svc = "hostapd-guest"
    try:
        if new_enabled:
            _run(["systemctl", "start", svc], timeout=15)
        else:
            _run(["systemctl", "stop", svc], timeout=15)
    except Exception:
        pass  # Best-effort; config is already updated

    return jsonify({"ok": True, "enabled": new_enabled})


# ── QoS endpoints ─────────────────────────────────────────────────────────────


def _read_qos_limits():
    """Read QoS limits from JSON store. Returns list."""
    try:
        return json.loads(Path(QOS_LIMITS_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return []


@app.route("/api/clients/qos", methods=["GET"])
@require_auth
def api_clients_qos_get():
    limits = _read_qos_limits()
    return jsonify({"limits": limits})


@app.route("/api/clients/qos", methods=["POST"])
@require_auth_always
def api_clients_qos_post():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    mac = data.get("mac", "")
    if not re.match(r'^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$', mac):
        return jsonify({"error": "Invalid MAC address format"}), 400

    if data.get("clear"):
        out, rc = _run([APPLY_QOS_SCRIPT, "uap0", mac, "clear"], timeout=10)
        if rc != 0:
            return jsonify({"error": f"apply-qos.sh failed (rc={rc})"}), 503
        return jsonify({"ok": True})

    down_kbps = data.get("down_kbps")
    up_kbps = data.get("up_kbps")
    if not isinstance(down_kbps, int) or not isinstance(up_kbps, int):
        return jsonify({"error": "down_kbps and up_kbps must be integers"}), 400
    if not (64 <= down_kbps <= 100000):
        return jsonify({"error": "down_kbps must be between 64 and 100000"}), 400
    if not (64 <= up_kbps <= 100000):
        return jsonify({"error": "up_kbps must be between 64 and 100000"}), 400

    out, rc = _run(
        [APPLY_QOS_SCRIPT, "uap0", mac, str(down_kbps), str(up_kbps)],
        timeout=10,
    )
    if rc != 0:
        return jsonify({"error": f"apply-qos.sh failed (rc={rc})"}), 503
    _push_event("qos_change", {"mac": mac})
    return jsonify({"ok": True})


# ── Config backup / restore ───────────────────────────────────────────────────

CONFIG_BACKUP_SCRIPT = "/usr/local/sbin/config-backup.sh"


@app.route("/api/system/backup", methods=["GET"])
@require_auth_always
def api_system_backup():
    """Run config-backup.sh and return the archive as a download."""
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz", prefix="travel-router-backup-")
    os.close(tmp_fd)
    try:
        result = subprocess.run(
            [CONFIG_BACKUP_SCRIPT, "backup", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr or "Backup script failed"}), 503

        filename = "travel-router-backup-" + datetime.now().strftime("%Y%m%d") + ".tar.gz"
        with open(tmp_path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return jsonify({"error": "config-backup.sh not installed"}), 503
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Backup timed out"}), 504
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return Response(
        data,
        mimetype="application/gzip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/api/system/restore", methods=["POST"])
@require_auth_always
def api_system_restore():
    """Accept a multipart backup upload and run config-backup.sh restore."""
    if "backup" not in request.files:
        return jsonify({"error": "Missing 'backup' file field"}), 400

    upload = request.files["backup"]
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz", prefix="travel-router-restore-")
    os.close(tmp_fd)
    try:
        upload.save(tmp_path)
        os.chmod(tmp_path, 0o600)

        result = subprocess.run(
            [CONFIG_BACKUP_SCRIPT, "restore", tmp_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr or result.stdout or "Restore script failed"}), 503

        restored_lines = [
            line.strip()
            for line in (result.stdout + result.stderr).splitlines()
            if line.strip()
        ]
        return jsonify({"ok": True, "restored_files": restored_lines})
    except FileNotFoundError:
        return jsonify({"error": "config-backup.sh not installed"}), 503
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Restore timed out"}), 504
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Captive portal ────────────────────────────────────────────────────────────


def _load_captive_creds() -> dict:
    """Read saved captive portal credentials. Returns {} on any error."""
    try:
        return json.loads(Path(CAPTIVE_CREDS_FILE).read_text())
    except Exception:
        return {}


def _save_captive_creds(url: str, username: str, password: str) -> None:
    """Atomically write captive portal credentials to disk."""
    try:
        data = {"url": url, "username": username, "password": password, "ts": int(time.time())}
        creds_dir = str(Path(CAPTIVE_CREDS_FILE).parent)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=creds_dir, prefix="captive-creds.")
        try:
            with os.fdopen(tmp_fd, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp_path, CAPTIVE_CREDS_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:
        pass


@app.route("/api/captive")
@require_auth
def api_captive_get():
    """Return captive portal detection state from captive-portal.json."""
    try:
        data = json.loads(Path(CAPTIVE_JSON).read_text())
    except (OSError, json.JSONDecodeError):
        data = {"detected": False}
    return jsonify(data)


@app.route("/api/captive/credentials", methods=["GET"])
@require_auth
def api_captive_credentials_get():
    """Return saved captive portal credentials (or {} if none)."""
    return jsonify(_load_captive_creds())


@app.route("/api/captive/credentials", methods=["POST"])
@require_auth_always
def api_captive_credentials_post():
    """Save captive portal credentials."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Expected JSON object"}), 400
    url = body.get("url", "")
    username = body.get("username", "")
    password = body.get("password", "")
    if not (isinstance(url, str) and url.strip()):
        return jsonify({"error": "Missing or empty 'url' field"}), 400
    if not (isinstance(username, str) and username.strip()):
        return jsonify({"error": "Missing or empty 'username' field"}), 400
    if not (isinstance(password, str) and password.strip()):
        return jsonify({"error": "Missing or empty 'password' field"}), 400
    _save_captive_creds(url.strip(), username.strip(), password.strip())
    return jsonify({"ok": True})


@app.route("/api/captive/credentials", methods=["DELETE"])
@require_auth_always
def api_captive_credentials_delete():
    """Remove saved captive portal credentials."""
    try:
        os.unlink(CAPTIVE_CREDS_FILE)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return jsonify({"error": f"Could not remove credentials file: {exc}"}), 503
    return jsonify({"ok": True})


@app.route("/api/captive/bypass", methods=["POST"])
@require_auth_always
def api_captive_bypass():
    """Attempt auto-login to a captive portal using provided credentials."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    url = body.get("url", "").strip()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()

    if not url:
        return jsonify({"error": "Missing 'url' field"}), 400
    # Basic URL sanity check — must start with http
    if not url.startswith("http"):
        return jsonify({"error": "Invalid URL — must start with http"}), 400
    # Reject shell metacharacters in all inputs
    for field_val in (url, username, password):
        if re.search(r'[;&|`$\'\"\\]', field_val):
            return jsonify({"error": "Invalid characters in request fields"}), 400

    cmd = [
        "curl", "-L",
        "-c", "/tmp/captive-cookies.txt",
        "-b", "/tmp/captive-cookies.txt",
        "--max-time", "15",
        "--data", f"username={username}&password={password}",
        url,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Request timed out"}), 504
    except OSError as exc:
        return jsonify({"error": f"curl not available: {exc}"}), 503

    if result.returncode != 0:
        return jsonify({"error": f"curl failed (rc={result.returncode}): {result.stderr[:200]}"}), 503

    # Auto-save credentials on successful bypass
    if url and username and password:
        _save_captive_creds(url, username, password)

    output = (result.stdout or "")[:1000]
    return jsonify({"ok": True, "output": output})


# ── Signal quality ────────────────────────────────────────────────────────────


def _parse_iwconfig(iface: str) -> dict:
    """Run iwconfig <iface> and parse quality/signal fields."""
    result: dict = {}
    try:
        out, rc = _run(f"iwconfig {iface} 2>/dev/null")
        if rc != 0 or not out.strip():
            return result
        m = re.search(r"Link Quality=(\d+)/(\d+)", out)
        if m:
            num, denom = int(m.group(1)), int(m.group(2))
            result["quality_pct"] = int(num / denom * 100) if denom else None
        m = re.search(r"Signal level=(-?\d+)", out)
        if m:
            result["signal_dbm"] = int(m.group(1))
    except Exception:
        pass
    return result


def _parse_iw_link(iface: str) -> dict:
    """Run iw dev <iface> link and parse SSID, bitrate, channel."""
    result: dict = {}
    try:
        out, rc = _run(["iw", "dev", iface, "link"])
        if rc != 0 or not out.strip():
            return result
        m = re.search(r"SSID: (.+)", out)
        if m:
            result["ssid"] = m.group(1).strip()
        m = re.search(r"tx bitrate: ([\d.]+)", out)
        if m:
            result["bitrate_mbps"] = float(m.group(1))
        m = re.search(r"channel (\d+)", out)
        if m:
            result["channel"] = int(m.group(1))
    except Exception:
        pass
    return result


def _parse_mmcli() -> dict:
    """Run mmcli -m 0 --output-keyvalue and parse LTE signal/operator/state."""
    result: dict = {}
    try:
        # Check if mmcli is available
        which_out, which_rc = _run("which mmcli")
        if which_rc != 0 or not which_out.strip():
            return result
        out, rc = _run("mmcli -m 0 --output-keyvalue 2>/dev/null", timeout=8)
        if rc != 0 or not out.strip():
            return result
        for line in out.splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            key, _, val = line.partition("|")
            key = key.strip()
            val = val.strip()
            if key == "signal-quality.value":
                try:
                    result["quality_pct"] = int(val)
                except ValueError:
                    pass
            elif key == "m3gpp.operator-name":
                result["operator"] = val
            elif key == "m3gpp.registration-state":
                result["state"] = val
    except Exception:
        pass
    return result


@app.route("/api/signal")
@require_auth
def api_signal():
    """Return WiFi uplink signal quality and optional LTE modem signal info."""
    try:
        # Determine active WiFi uplink iface: try wlan1 (client-mode) first, then wlan0
        wifi_iface = None
        for candidate in ("wlan1", "wlan0"):
            chk_out, chk_rc = _run(["ip", "link", "show", candidate])
            if chk_rc == 0 and chk_out.strip():
                wifi_iface = candidate
                break

        wifi: dict = {"quality_pct": None, "signal_dbm": None, "ssid": None,
                      "channel": None, "bitrate_mbps": None}
        if wifi_iface:
            iw_data = _parse_iwconfig(wifi_iface)
            wifi.update(iw_data)
            link_data = _parse_iw_link(wifi_iface)
            wifi.update(link_data)

        # LTE modem via ModemManager
        lte_raw = _parse_mmcli()
        lte: dict | None = None
        if lte_raw:
            lte = {
                "quality_pct": lte_raw.get("quality_pct"),
                "operator": lte_raw.get("operator"),
                "state": lte_raw.get("state"),
            }

        # Determine active uplink type
        active = "wifi"
        uplink_out, _ = _run("ip route show default")
        m = re.search(r"dev\s+(\S+)", uplink_out)
        if m:
            dev = m.group(1)
            if dev in ("usb0", "rndis0") or dev.startswith("enx"):
                active = "usb"
            elif dev == wifi_iface:
                active = "wifi"
            elif lte and dev in ("wwan0",):
                active = "lte"

        return jsonify({"wifi": wifi, "lte": lte, "active": active})
    except Exception:
        return jsonify({"wifi": None, "lte": None, "active": None})


# ── Scheduled reboot ─────────────────────────────────────────────────────────

SCHEDULE_REBOOT_SCRIPT = "/usr/local/sbin/schedule-reboot.sh"


@app.route("/api/schedule/reboot", methods=["GET"])
@require_auth
def api_schedule_reboot_get():
    out, rc = _run([SCHEDULE_REBOOT_SCRIPT, "status"], timeout=5)
    try:
        data = json.loads(out.strip())
    except (json.JSONDecodeError, ValueError):
        data = {"enabled": False, "time": None, "skip_if_clients": False}
    return jsonify(data)


@app.route("/api/schedule/reboot", methods=["POST"])
@require_auth_always
def api_schedule_reboot_post():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    enabled = body.get("enabled", False)

    if not enabled:
        _run([SCHEDULE_REBOOT_SCRIPT, "clear"], timeout=5)
        return jsonify({"ok": True})

    time_str = body.get("time", "")
    if not re.match(r'^\d{2}:\d{2}$', str(time_str)):
        return jsonify({"error": "Invalid time format — expected HH:MM"}), 400

    parts = str(time_str).split(":")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23):
        return jsonify({"error": "Hour must be 0-23"}), 400
    if not (0 <= mm <= 59):
        return jsonify({"error": "Minute must be 0-59"}), 400

    skip_if_clients = bool(body.get("skip_if_clients", False))
    cmd = [SCHEDULE_REBOOT_SCRIPT, "set", str(time_str)]
    if skip_if_clients:
        cmd.append("--skip-if-clients")

    out, rc = _run(cmd, timeout=5)
    if rc != 0:
        return jsonify({"error": f"schedule-reboot.sh failed (rc={rc})"}), 503

    return jsonify({"ok": True})


# ── Uplink reconnect ──────────────────────────────────────────────────────────


@app.route("/api/uplink/reconnect", methods=["POST"])
@require_auth_always
def api_uplink_reconnect():
    try:
        subprocess.run(
            ["ip", "link", "set", "wlan1", "down"],
            capture_output=True, timeout=5,
        )
        import time as _t
        _t.sleep(2)
        subprocess.run(
            ["ip", "link", "set", "wlan1", "up"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass  # best-effort

    try:
        subprocess.run(
            ["systemctl", "restart", "wpa_supplicant@wlan1"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass  # best-effort

    return jsonify({"ok": True, "message": "Reconnecting uplink…"})


# ── DNS-over-HTTPS resolver ───────────────────────────────────────────────────


@app.route("/api/doh", methods=["GET"])
@require_auth
def api_doh_get():
    resolver = _read_defaults_value("DOH_RESOLVER", "system")
    return jsonify({"resolver": resolver or "system", "presets": DOH_PRESETS})


@app.route("/api/doh", methods=["POST"])
@require_auth_always
def api_doh_post():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    resolver = data.get("resolver", "").strip()
    if not resolver:
        return jsonify({"error": "Missing 'resolver' field"}), 400

    # Validate: must be a known preset or a https:// URL
    if resolver not in DOH_PRESETS:
        if not resolver.startswith("https://"):
            return jsonify({"error": "resolver must be a preset name or a https:// URL"}), 400
        # Reject shell metacharacters in URL
        if re.search(r'[;&|`$\'\"\\<>]', resolver):
            return jsonify({"error": "Invalid characters in resolver URL"}), 400
        if len(resolver) > 512:
            return jsonify({"error": "resolver URL too long"}), 400

    try:
        result = subprocess.run(
            [DOH_SCRIPT, resolver],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return jsonify({"error": "set-doh-resolver.sh not installed"}), 503
    except subprocess.TimeoutExpired:
        return jsonify({"error": "set-doh-resolver.sh timed out"}), 504

    if result.returncode != 0:
        return jsonify({"error": result.stderr or result.stdout or "Script failed"}), 503

    _push_event("doh_change", {"resolver": resolver})
    return jsonify({"ok": True})


# ── Traceroute ────────────────────────────────────────────────────────────────


@app.route("/api/traceroute", methods=["POST"])
@require_auth_always
def api_traceroute():
    """Run traceroute to a target host/IP and return per-hop latency."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    target = data.get("target", "")
    if not isinstance(target, str) or not target:
        return jsonify({"error": "Missing or invalid 'target' field"}), 400

    # Reject shell metacharacters
    if re.search(r'[;&|`$\'"\\<>\n]', target):
        return jsonify({"error": "Invalid characters in target"}), 400

    # Length limit
    if len(target) > 253:
        return jsonify({"error": "Target too long (max 253 chars)"}), 400

    # Must look like a hostname or IP
    if not re.match(r'^[a-zA-Z0-9.\-]+$', target):
        return jsonify({"error": "Invalid target format"}), 400

    try:
        result = subprocess.run(
            ["traceroute", "-n", "-m", "15", "-w", "2", target],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return jsonify({"error": "traceroute not installed"}), 503
    except subprocess.TimeoutExpired:
        return jsonify({"error": "traceroute timed out"}), 504

    hops = []
    for line in result.stdout.splitlines():
        # Match lines like: " 1  10.0.0.1  1.234 ms  1.345 ms  1.456 ms"
        # or star hops:     " 2  * * *"
        m = re.match(r'^\s*(\d+)\s+(.*)', line)
        if not m:
            continue
        hop_num = int(m.group(1))
        rest = m.group(2).strip()
        # Extract IP: first token (may be * or an IP address)
        tokens = rest.split()
        if not tokens:
            continue
        first = tokens[0]
        # Star hop
        if first == "*":
            hops.append({"hop": hop_num, "ip": "*", "rtts_ms": []})
            continue
        # Valid IP
        ip = first
        # Extract all RTT values (floats before " ms")
        rtts = [float(v) for v in re.findall(r'([\d.]+)\s+ms', rest)]
        hops.append({"hop": hop_num, "ip": ip, "rtts_ms": rtts})

    return jsonify({"ok": True, "target": target, "hops": hops})


# ── Storage (USB/SD) ─────────────────────────────────────────────────────────

_DEVICE_RE = re.compile(r'^[a-z0-9]+$')


def _parse_df_travel_data():
    """Run df -h /media/travel-data and return (used_gb, free_gb, total_gb) or (None, None, None)."""
    out, rc = _run("df -h /media/travel-data 2>/dev/null")
    if rc != 0 or not out.strip():
        return None, None, None
    try:
        lines = out.strip().splitlines()
        if len(lines) < 2:
            return None, None, None
        parts = lines[1].split()
        # df -h columns: Filesystem  Size  Used  Avail  Use%  Mounted
        if len(parts) < 5:
            return None, None, None

        def _to_gb(s):
            s = s.upper()
            if s.endswith("G"):
                return round(float(s[:-1]), 1)
            if s.endswith("M"):
                return round(float(s[:-1]) / 1024, 2)
            if s.endswith("K"):
                return round(float(s[:-1]) / (1024 * 1024), 3)
            if s.endswith("T"):
                return round(float(s[:-1]) * 1024, 1)
            try:
                return round(float(s) / 1e9, 1)
            except ValueError:
                return None

        total_gb = _to_gb(parts[1])
        used_gb = _to_gb(parts[2])
        free_gb = _to_gb(parts[3])
        return used_gb, free_gb, total_gb
    except Exception:
        return None, None, None


@app.route("/api/storage")
@require_auth
def api_storage_get():
    """Return removable block devices and current mount status of /media/travel-data."""
    # Run lsblk to list devices
    devices = []
    lsblk_out, lsblk_rc = _run(
        "lsblk -J -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,LABEL,RM 2>/dev/null"
    )
    if lsblk_rc == 0 and lsblk_out.strip():
        try:
            data = json.loads(lsblk_out)
            for bd in data.get("blockdevices", []):
                def _add_device(dev):
                    """Recursively collect removable partitions/disks."""
                    dev_type = dev.get("type", "")
                    removable = str(dev.get("rm", "false")).lower() in ("true", "1")
                    if removable and dev_type in ("part", "disk"):
                        devices.append({
                            "name": dev.get("name", ""),
                            "size": dev.get("size", ""),
                            "fstype": dev.get("fstype") or "",
                            "label": dev.get("label") or "",
                            "removable": True,
                            "mountpoint": dev.get("mountpoint") or "",
                        })
                    for child in dev.get("children", []):
                        _add_device(child)
                _add_device(bd)
        except (json.JSONDecodeError, KeyError):
            pass

    # Check if /media/travel-data is mounted
    mount_check_out, mount_check_rc = _run(
        "findmnt -n /media/travel-data 2>/dev/null"
    )
    mounted = mount_check_rc == 0 and bool(mount_check_out.strip())

    used_gb, free_gb, total_gb = _parse_df_travel_data() if mounted else (None, None, None)

    return jsonify({
        "mounted": mounted,
        "mount_point": "/media/travel-data",
        "used_gb": used_gb,
        "free_gb": free_gb,
        "total_gb": total_gb,
        "devices": devices,
    })


@app.route("/api/storage/mount", methods=["POST"])
@require_auth_always
def api_storage_mount():
    """Mount a removable device to /media/travel-data."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    device = str(body.get("device", "")).strip()
    if not device:
        return jsonify({"error": "Missing 'device' field"}), 400
    if not _DEVICE_RE.match(device) or len(device) > 20:
        return jsonify({"error": "Invalid device name — only [a-z0-9], max 20 chars"}), 400

    try:
        result = subprocess.run(
            [MOUNT_STORAGE_SCRIPT, "mount", device],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return jsonify({"error": "mount-storage.sh not installed"}), 503
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Mount timed out"}), 504

    if result.returncode != 0:
        return jsonify({"error": (result.stderr or result.stdout or "Mount failed").strip()}), 503

    return jsonify({"ok": True})


@app.route("/api/storage/unmount", methods=["POST"])
@require_auth_always
def api_storage_unmount():
    """Unmount /media/travel-data."""
    try:
        result = subprocess.run(
            [MOUNT_STORAGE_SCRIPT, "unmount"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return jsonify({"error": "mount-storage.sh not installed"}), 503
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Unmount timed out"}), 504

    if result.returncode != 0:
        return jsonify({"error": (result.stderr or result.stdout or "Unmount failed").strip()}), 503

    return jsonify({"ok": True})


# ── Wake-on-LAN ───────────────────────────────────────────────────────────────

_MAC_RE = re.compile(r'^([0-9a-fA-F]{2}[:\-]?){5}[0-9a-fA-F]{2}$')


def _read_wol_targets() -> list:
    """Read WoL targets from JSON store. Returns [] on any error."""
    try:
        data = json.loads(Path(WOL_TARGETS_FILE).read_text())
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _write_wol_targets(targets: list) -> None:
    """Atomically write WoL targets list to disk."""
    wol_dir = str(Path(WOL_TARGETS_FILE).parent)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=wol_dir, prefix="wol-targets.")
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            json.dump(targets, fh)
        os.replace(tmp_path, WOL_TARGETS_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _send_magic_packet(mac: str, broadcast: str = "255.255.255.255") -> None:
    """Send a Wake-on-LAN magic packet to the given MAC address."""
    import socket
    # Normalize: strip colons and hyphens
    mac_clean = mac.replace(":", "").replace("-", "")
    if len(mac_clean) != 12 or not all(c in "0123456789abcdefABCDEF" for c in mac_clean):
        raise ValueError(f"Invalid MAC address: {mac!r}")
    mac_bytes = bytes.fromhex(mac_clean)
    magic = b'\xff' * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic, (broadcast, 9))


@app.route("/api/wol", methods=["GET"])
@require_auth
def api_wol_get():
    return jsonify({"targets": _read_wol_targets()})


@app.route("/api/wol/send", methods=["POST"])
@require_auth_always
def api_wol_send():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400
    mac = data.get("mac", "")
    if not isinstance(mac, str) or not _MAC_RE.match(mac):
        return jsonify({"error": "Invalid MAC address format"}), 400
    broadcast = data.get("broadcast", "255.255.255.255")
    if not isinstance(broadcast, str):
        broadcast = "255.255.255.255"
    try:
        _send_magic_packet(mac, broadcast)
    except Exception as exc:
        return jsonify({"error": f"Failed to send magic packet: {exc}"}), 503
    return jsonify({"ok": True})


@app.route("/api/wol/targets", methods=["POST"])
@require_auth_always
def api_wol_targets_post():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400
    targets = data.get("targets", [])
    if not isinstance(targets, list):
        return jsonify({"error": "'targets' must be a list"}), 400
    if len(targets) > 20:
        return jsonify({"error": "Maximum 20 targets allowed"}), 400
    validated = []
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            return jsonify({"error": f"Target {i} must be an object"}), 400
        name = t.get("name", "")
        if not isinstance(name, str) or len(name) == 0 or len(name) > 64:
            return jsonify({"error": f"Target {i}: 'name' must be a non-empty string (max 64 chars)"}), 400
        mac = t.get("mac", "")
        if not isinstance(mac, str) or not _MAC_RE.match(mac):
            return jsonify({"error": f"Target {i}: invalid MAC address format"}), 400
        broadcast = t.get("broadcast", "255.255.255.255")
        if not isinstance(broadcast, str) or not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', broadcast):
            return jsonify({"error": f"Target {i}: invalid broadcast IPv4 address"}), 400
        validated.append({"name": name, "mac": mac, "broadcast": broadcast})
    try:
        _write_wol_targets(validated)
    except Exception as exc:
        return jsonify({"error": f"Failed to save targets: {exc}"}), 503
    return jsonify({"ok": True})


# ── Data cap / monthly budget tracker ────────────────────────────────────────

_DATACAP_DEFAULT = {
    "cap_mb": 0,
    "iface": "wlan1",
    "reset_day": 1,
    "baseline_bytes": 0,
    "reset_ts": 0,
}


def _read_datacap() -> dict:
    try:
        data = json.loads(Path(DATACAP_FILE).read_text())
        if isinstance(data, dict):
            result = dict(_DATACAP_DEFAULT)
            result.update(data)
            return result
    except Exception:
        pass
    return dict(_DATACAP_DEFAULT)


def _write_datacap(data: dict) -> None:
    try:
        cap_dir = str(Path(DATACAP_FILE).parent)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=cap_dir, prefix="datacap.")
        try:
            with os.fdopen(tmp_fd, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp_path, DATACAP_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:
        pass


def _current_rx_bytes(iface: str) -> int:
    try:
        text = Path("/proc/net/dev").read_text()
        for line in text.splitlines()[2:]:
            parts = line.split(":")
            if len(parts) != 2:
                continue
            if parts[0].strip() == iface:
                fields = parts[1].split()
                if fields:
                    return int(fields[0])
    except Exception:
        pass
    return 0


@app.route("/api/datacap", methods=["GET"])
@require_auth
def api_datacap_get():
    cfg = _read_datacap()
    cap_mb = cfg.get("cap_mb", 0)
    iface = cfg.get("iface", "wlan1")
    reset_day = cfg.get("reset_day", 1)
    baseline_bytes = cfg.get("baseline_bytes", 0)
    reset_ts = cfg.get("reset_ts", 0)

    now = int(time.time())
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    reset_dt = datetime.fromtimestamp(reset_ts, tz=timezone.utc) if reset_ts else None

    # Auto-reset if today >= reset_day and last reset was from a previous month
    if now_dt.day >= reset_day and (
        reset_dt is None
        or (reset_dt.year, reset_dt.month) < (now_dt.year, now_dt.month)
    ):
        baseline_bytes = _current_rx_bytes(iface)
        reset_ts = now
        cfg["baseline_bytes"] = baseline_bytes
        cfg["reset_ts"] = reset_ts
        _write_datacap(cfg)

    current_rx = _current_rx_bytes(iface)
    raw_used = current_rx - baseline_bytes
    used_mb = max(0.0, raw_used / 1024 / 1024)

    pct = 0.0
    if cap_mb and cap_mb > 0:
        pct = round(used_mb / cap_mb * 100, 1)

    return jsonify({
        "cap_mb": cap_mb,
        "used_mb": round(used_mb, 2),
        "iface": iface,
        "reset_day": reset_day,
        "pct": pct,
        "warning": bool(cap_mb and used_mb > cap_mb * 0.8),
    })


@app.route("/api/datacap", methods=["POST"])
@require_auth_always
def api_datacap_post():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    cap_mb = data.get("cap_mb")
    iface = data.get("iface", "wlan1")
    reset_day = data.get("reset_day", 1)

    if not isinstance(cap_mb, int) or not (0 <= cap_mb <= 100000):
        return jsonify({"error": "cap_mb must be an integer between 0 and 100000"}), 400
    if not isinstance(reset_day, int) or not (1 <= reset_day <= 28):
        return jsonify({"error": "reset_day must be an integer between 1 and 28"}), 400
    if not isinstance(iface, str) or not re.match(r'^[a-z0-9]+$', iface) or len(iface) > 15:
        return jsonify({"error": "iface must be lowercase alphanumeric, max 15 chars"}), 400

    existing = _read_datacap()
    existing["cap_mb"] = cap_mb
    existing["iface"] = iface
    existing["reset_day"] = reset_day
    _write_datacap(existing)

    return jsonify({"ok": True})


# ── Device aliases ────────────────────────────────────────────────────────────

_ALIAS_MAC_RE = re.compile(r'^([0-9a-fA-F]{2}[:\-]?){5}[0-9a-fA-F]{2}$')


def _read_aliases() -> dict:
    """Returns {mac_lower: friendly_name} dict."""
    try:
        text = Path(ALIASES_FILE).read_text().strip()
        if not text:
            return {}
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _write_aliases(aliases: dict) -> None:
    """Atomically writes aliases dict to ALIASES_FILE."""
    aliases_dir = str(Path(ALIASES_FILE).parent)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=aliases_dir, prefix="aliases.")
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            json.dump(aliases, fh)
        os.replace(tmp_path, ALIASES_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@app.route("/api/clients/aliases", methods=["GET"])
@require_auth
def api_clients_aliases_get():
    return jsonify({"aliases": _read_aliases()})


@app.route("/api/clients/aliases", methods=["POST"])
@require_auth_always
def api_clients_aliases_post():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    mac = data.get("mac", "")
    if not isinstance(mac, str) or not _ALIAS_MAC_RE.match(mac):
        return jsonify({"error": "Invalid MAC address format"}), 400

    # Normalize MAC to lowercase colon-separated
    mac_norm = mac.lower().replace("-", ":").replace(" ", "")
    # Ensure proper colon-separated format (handle no-separator input)
    if ":" not in mac_norm:
        mac_norm = ":".join(mac_norm[i:i+2] for i in range(0, 12, 2))

    name = data.get("name", "")
    if not isinstance(name, str):
        return jsonify({"error": "name must be a string"}), 400
    name = name.strip()

    # Validate name
    if len(name) > 32:
        return jsonify({"error": "name must be at most 32 characters"}), 400
    if re.search(r'[<>"]', name):
        return jsonify({"error": "name must not contain <, >, or \" characters"}), 400

    aliases = _read_aliases()

    if not name:
        # Empty/blank name → delete the alias
        aliases.pop(mac_norm, None)
    else:
        aliases[mac_norm] = name

    try:
        _write_aliases(aliases)
    except Exception as exc:
        return jsonify({"error": f"Failed to save aliases: {exc}"}), 503

    return jsonify({"ok": True, "aliases": aliases})


# ── Latency history endpoint ──────────────────────────────────────────────────


@app.route("/api/latency/history")
@require_auth
def api_latency_history():
    with _LATENCY_LOCK:
        history = list(_LATENCY_HISTORY[-_LATENCY_MAX_ENTRIES:])
    last_gateway = None
    for entry in reversed(history):
        if entry.get("gateway"):
            last_gateway = entry["gateway"]
            break
    return jsonify({"history": history, "gateway": last_gateway})


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
    _push_event("privacy_change", {"profile": profile})
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


@app.route('/api/system/speedtest', methods=['GET'])
@require_auth
def api_speedtest_get():
    """Return last cached speed test result without running a new test."""
    cached = _SPEEDTEST_RESULT.get("result")
    if cached is None:
        return jsonify({"cached": False, "result": None})
    age = int(time.time() - _SPEEDTEST_RESULT["ts"])
    return jsonify({
        "cached": True,
        "age_seconds": age,
        "download_mbps": cached.get("download_mbps"),
        "upload_mbps": cached.get("upload_mbps"),
        "ping_ms": cached.get("ping_ms"),
        "server": cached.get("server"),
        "method": cached.get("method"),
        "ts": _SPEEDTEST_RESULT["ts"],
    })


@app.route('/api/system/speedtest', methods=['POST'])
@require_auth_always
def api_speedtest_post():
    """Run a speed test synchronously and cache+return the result."""
    script = "/usr/local/sbin/speedtest.sh"
    try:
        result = subprocess.run(
            [script],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except FileNotFoundError:
        return jsonify({"error": "speedtest.sh not found"}), 503
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Speed test timed out"}), 504

    if result.returncode != 0:
        return jsonify({"error": result.stderr or "Speed test failed"}), 503

    try:
        data = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return jsonify({"error": "Failed to parse speed test output"}), 503

    _SPEEDTEST_RESULT["ts"] = time.time()
    _SPEEDTEST_RESULT["result"] = data

    return jsonify({
        "ok": True,
        "cached": False,
        "download_mbps": data.get("download_mbps"),
        "upload_mbps": data.get("upload_mbps"),
        "ping_ms": data.get("ping_ms"),
        "server": data.get("server"),
        "method": data.get("method"),
    })


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
