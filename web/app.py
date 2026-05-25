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
from uuid import uuid4

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
PORT_FORWARD_FILE = "/var/lib/travel-router/portforward.json"
SPEEDTEST_HISTORY_FILE = "/var/lib/travel-router/speedtest-history.json"

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

# ── System resource history sampler ──────────────────────────────────────────

_RESOURCE_HISTORY: list = []
_RESOURCE_LOCK = threading.Lock()
_RESOURCE_SAMPLE_INTERVAL = 30   # seconds
_RESOURCE_MAX_ENTRIES = 120      # ~1 hour at 30-second intervals

# Previous /proc/stat totals for CPU delta calculation
_prev_cpu_idle: float = 0.0
_prev_cpu_total: float = 0.0


def _read_cpu_pct() -> float:
    """Return CPU usage % since last call, from /proc/stat delta. Returns 0.0 on error."""
    global _prev_cpu_idle, _prev_cpu_total
    try:
        text = Path("/proc/stat").read_text()
        for line in text.splitlines():
            if line.startswith("cpu "):
                fields = line.split()[1:]  # skip "cpu" label
                if len(fields) < 4:
                    return 0.0
                user = float(fields[0])
                nice = float(fields[1])
                system = float(fields[2])
                idle = float(fields[3])
                iowait = float(fields[4]) if len(fields) > 4 else 0.0
                total = user + nice + system + idle + iowait + sum(float(f) for f in fields[5:])
                idle_total = idle + iowait

                delta_total = total - _prev_cpu_total
                delta_idle = idle_total - _prev_cpu_idle

                _prev_cpu_total = total
                _prev_cpu_idle = idle_total

                if delta_total <= 0:
                    return 0.0
                return round(100.0 - (delta_idle / delta_total * 100.0), 1)
        return 0.0
    except Exception:
        return 0.0


def _read_mem_pct() -> float:
    """Return memory usage % from /proc/meminfo. Returns 0.0 on error."""
    try:
        meminfo = Path("/proc/meminfo").read_text()
        mem = {}
        for line in meminfo.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                mem[parts[0].rstrip(":")] = int(parts[1])
        mem_total = mem.get("MemTotal", 0)
        mem_avail = mem.get("MemAvailable", 0)
        if mem_total <= 0:
            return 0.0
        return round((mem_total - mem_avail) / mem_total * 100.0, 1)
    except Exception:
        return 0.0


def _read_disk_pct() -> float:
    """Return root filesystem usage % via os.statvfs. Returns 0.0 on error."""
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        if total <= 0:
            return 0.0
        used = total - free
        return round(used / total * 100.0, 1)
    except Exception:
        return 0.0


def _read_temp_c():
    """Return CPU temperature in °C from thermal_zone0, or None if unavailable."""
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return round(int(raw) / 1000.0, 1)
    except Exception:
        return None


def _resource_sample_once():
    """Take one system resource sample and append to _RESOURCE_HISTORY."""
    sample = {
        "ts": int(time.time()),
        "cpu_pct": _read_cpu_pct(),
        "mem_pct": _read_mem_pct(),
        "disk_pct": _read_disk_pct(),
        "temp_c": _read_temp_c(),
    }
    with _RESOURCE_LOCK:
        _RESOURCE_HISTORY.append(sample)
        if len(_RESOURCE_HISTORY) > _RESOURCE_MAX_ENTRIES:
            del _RESOURCE_HISTORY[:-_RESOURCE_MAX_ENTRIES]


def _resource_sampler_loop():
    """Background thread: sample system resources every _RESOURCE_SAMPLE_INTERVAL seconds."""
    while True:
        time.sleep(_RESOURCE_SAMPLE_INTERVAL)
        try:
            _resource_sample_once()
        except Exception:
            pass  # Never crash the background thread


# Start background resource sampler when the module loads
threading.Thread(target=_resource_sampler_loop, daemon=True).start()

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
    search_term = request.args.get("search", "").strip()
    filter_level = request.args.get("level", "").strip().lower()

    if len(search_term) > 128:
        return jsonify({"error": "search param too long (max 128 chars)"}), 400

    valid_levels = {"emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"}
    if filter_level and filter_level not in valid_levels:
        return jsonify({"error": f"invalid level; choose from {sorted(valid_levels)}"}), 400

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

    if search_term:
        all_lines = [l for l in all_lines if search_term.lower() in l.lower()]
    if filter_level and filter_level in valid_levels:
        all_lines = [l for l in all_lines if filter_level in l.lower()]
    filtered = bool(search_term or filter_level)

    return jsonify({"lines": all_lines[-limit:], "total": len(all_lines), "filtered": filtered})


@app.route("/api/logs/export", methods=["GET"])
@require_auth
def api_logs_export():
    """Return combined travel router logs as a downloadable text file."""
    import io as _io
    fmt = request.args.get("format", "txt")
    lines_limit = min(int(request.args.get("lines", 5000)), 20000)
    # Collect from known log sources (same as /api/logs)
    log_lines = []
    log_files = [
        "/var/log/travel-router/wan-watchdog.log",
        "/var/log/travel-router/travel-router-web.log",
        "/var/log/syslog",
        "/var/log/messages",
    ]
    for log_file in log_files:
        try:
            text = Path(log_file).read_text()
            # Get last N lines from each file
            file_lines = text.splitlines()[-1000:]
            log_lines.extend(f"=== {log_file} ===\n" + l for l in file_lines)
            log_lines.append("")
        except OSError:
            continue
    # Trim to limit
    combined = "\n".join(log_lines[-lines_limit:])
    if not combined:
        # Try journalctl as fallback
        try:
            out, _ = _run(["journalctl", "-n", str(min(lines_limit, 5000)), "--no-pager", "-u", "travel-router-web", "--output=short-iso"], timeout=10)
            combined = out or "(no logs available)"
        except Exception:
            combined = "(no logs available)"
    buf = _io.BytesIO(combined.encode("utf-8", errors="replace"))
    buf.seek(0)
    import datetime as _dt
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"travel-router-logs-{ts}.txt"
    from flask import send_file as _send_file
    return _send_file(buf, as_attachment=True, download_name=filename, mimetype="text/plain")


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


# ── Service control panel ─────────────────────────────────────────────────────

_SERVICE_PANEL_UNITS = [
    "hostapd", "dnsmasq", "wg-quick@wg0", "tailscaled",
    "travel-router-web", "systemd-networkd", "dhcpcd",
    "failover-watchdog", "wan-watchdog",
]

@app.route("/api/services/status", methods=["GET"])
@require_auth
def api_services_status():
    """Return active/inactive/failed status for key router services."""
    statuses = []
    for unit in _SERVICE_PANEL_UNITS:
        out, _ = _run(["systemctl", "is-active", unit], timeout=3)
        state = (out or "").strip()
        # systemctl is-active returns: active, inactive, failed, activating, deactivating, unknown
        statuses.append({"unit": unit, "state": state})
    return jsonify({"services": statuses})


@app.route("/api/system/reboot", methods=["POST"])
@require_auth
def api_system_reboot():
    """Schedule a system reboot in 10 seconds (gives client time to show countdown)."""
    import threading as _t
    import time as _time
    def _do_reboot():
        _time.sleep(10)
        _run(["systemctl", "reboot"])
    _t.Thread(target=_do_reboot, daemon=True).start()
    return jsonify({"scheduled": True, "action": "reboot", "in_seconds": 10})


@app.route("/api/system/shutdown", methods=["POST"])
@require_auth
def api_system_shutdown():
    """Schedule a system shutdown in 10 seconds."""
    import threading as _t
    import time as _time
    def _do_shutdown():
        _time.sleep(10)
        _run(["systemctl", "poweroff"])
    _t.Thread(target=_do_shutdown, daemon=True).start()
    return jsonify({"scheduled": True, "action": "shutdown", "in_seconds": 10})


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

    qr_data_url = None
    try:
        result = subprocess.run(
            ["qrencode", "-t", "PNG", "-o", "-"],
            input=client_conf,
            capture_output=True,
            text=False,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            import base64 as _b64
            qr_data_url = "data:image/png;base64," + _b64.b64encode(result.stdout).decode()
    except FileNotFoundError:
        pass  # qrencode not installed — return config without QR image
    except Exception:
        pass

    return jsonify({
        "pubkey": pubkey,
        "client_config": client_conf,
        "qr_data_url": qr_data_url,
        "note": "Replace REPLACE_WITH_CLIENT_PRIVATE_KEY with the peer's private key before scanning.",
    })


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


# ── WiFi QR code ──────────────────────────────────────────────────────────────


@app.route("/api/wifi/qr", methods=["GET"])
@require_auth
def api_wifi_qr():
    """Return WiFi QR code as a PNG data-URL and the raw WiFi string."""
    # Read SSID and WPA passphrase from /etc/raspap/hostapd.ini or hostapd.conf
    ssid = "TravelRouter"
    password = ""
    try:
        # Try RaspAP config first
        conf_path = Path("/etc/raspap/hostapd.ini")
        if not conf_path.exists():
            conf_path = Path("/etc/hostapd/hostapd.conf")
        text = conf_path.read_text()
        for line in text.splitlines():
            if line.startswith("ssid="):
                ssid = line.split("=", 1)[1].strip()
            elif line.startswith("wpa_passphrase="):
                password = line.split("=", 1)[1].strip()
    except OSError:
        pass
    # Build WiFi QR string: WIFI:T:WPA;S:<ssid>;P:<pass>;;
    wifi_str = f"WIFI:T:WPA;S:{ssid};P:{password};;"
    # Try to generate QR code PNG via qrencode
    qr_data_url = None
    try:
        import base64
        result, rc = _run(["qrencode", "-o", "-", "-t", "PNG", "--size=6", wifi_str], timeout=5)
        if result:
            qr_data_url = "data:image/png;base64," + base64.b64encode(
                result.encode() if isinstance(result, str) else result
            ).decode()
    except Exception:
        pass
    # Also try generating as SVG fallback
    if not qr_data_url:
        try:
            import base64
            result, _ = _run(["qrencode", "-o", "-", "-t", "SVG", wifi_str], timeout=5)
            if result:
                qr_data_url = "data:image/svg+xml;base64," + base64.b64encode(
                    result.encode() if isinstance(result, str) else result
                ).decode()
        except Exception:
            pass
    return jsonify({
        "ssid": ssid,
        "wifi_string": wifi_str,
        "qr_data_url": qr_data_url,
        "has_qrencode": qr_data_url is not None,
    })


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


# ── Reboot schedule ───────────────────────────────────────────────────────────

REBOOT_SCHEDULE_CONF = "/etc/travel-router/reboot-schedule.conf"
REBOOT_CRON_FILE = "/etc/cron.d/travel-router-reboot"


def _read_reboot_schedule() -> dict:
    """Read reboot schedule config. Returns {enabled, hour, minute, next_reboot}."""
    enabled = False
    hour = 3
    minute = 30
    # Check if cron file exists and is not commented out
    try:
        text = Path(REBOOT_CRON_FILE).read_text()
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        minute = int(parts[0])
                        hour = int(parts[1])
                        enabled = True
                    except ValueError:
                        pass
    except OSError:
        pass
    # Also check conf file
    try:
        text = Path(REBOOT_SCHEDULE_CONF).read_text()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("REBOOT_TIME="):
                t = line.split("=", 1)[1].strip().strip('"').strip("'")
                if ":" in t:
                    try:
                        h, m = t.split(":", 1)
                        hour = int(h)
                        minute = int(m)
                    except ValueError:
                        pass
            elif line.startswith("ENABLE_SCHEDULED_REBOOT="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
                enabled = val in ("1", "true", "yes")
    except OSError:
        pass
    # Calculate next reboot time
    import datetime as _dt
    now = _dt.datetime.now()
    next_reboot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_reboot <= now:
        next_reboot += _dt.timedelta(days=1)
    return {
        "enabled": enabled,
        "hour": hour,
        "minute": minute,
        "time_str": f"{hour:02d}:{minute:02d}",
        "next_reboot": next_reboot.isoformat(),
    }


@app.route("/api/system/reboot-schedule", methods=["GET"])
@require_auth
def api_reboot_schedule_get():
    return jsonify(_read_reboot_schedule())


@app.route("/api/system/reboot-schedule", methods=["POST"])
@require_auth_always
def api_reboot_schedule_post():
    """Update reboot schedule."""
    body = request.get_json(silent=True) or {}
    enabled = bool(body.get("enabled", True))
    try:
        hour = int(body.get("hour", 3))
        minute = int(body.get("minute", 30))
    except (ValueError, TypeError):
        return jsonify({"error": "invalid hour or minute"}), 400
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return jsonify({"error": "hour must be 0-23, minute 0-59"}), 400
    # Run schedule-reboot.sh if it exists, else write cron directly
    try:
        result = _run(
            ["bash", "/usr/local/sbin/schedule-reboot.sh",
             f"{hour:02d}:{minute:02d}", "1" if enabled else "0"],
            timeout=10,
        )
    except Exception:
        result = None
    if result is None or not result[0]:
        # Fallback: write cron file directly
        try:
            cron_content = f"{minute} {hour} * * * root /sbin/reboot\n" if enabled else \
                           f"# {minute} {hour} * * * root /sbin/reboot  (disabled)\n"
            d = str(Path(REBOOT_CRON_FILE).parent)
            fd, tmp = tempfile.mkstemp(dir=d)
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(cron_content)
                os.replace(tmp, REBOOT_CRON_FILE)
            except Exception:
                import contextlib
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "enabled": enabled, "time": f"{hour:02d}:{minute:02d}"})


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


# ── Uplink history ────────────────────────────────────────────────────────────

UPLINK_HISTORY_FILE = "/var/lib/travel-router/uplink-history.json"
_uplink_history_lock = threading.Lock()


def _get_current_uplink() -> dict:
    """Detect current default route interface and its type."""
    try:
        out, _ = _run(["ip", "route", "show", "default"], timeout=3)
        for line in (out or "").splitlines():
            parts = line.split()
            if "dev" in parts:
                idx = parts.index("dev")
                iface = parts[idx + 1] if idx + 1 < len(parts) else ""
                # Classify interface type
                if iface.startswith("wg"):
                    itype = "wireguard"
                elif iface.startswith("tun") or iface.startswith("tap"):
                    itype = "vpn"
                elif iface.startswith("bnep") or iface.startswith("bt"):
                    itype = "bluetooth"
                elif iface.startswith("enx") or iface.startswith("usb") or iface.startswith("rndis"):
                    itype = "usb-tether"
                elif iface.startswith("wlan"):
                    itype = "wifi"
                elif iface.startswith("eth"):
                    itype = "ethernet"
                else:
                    itype = "other"
                # Get metric
                metric = ""
                if "metric" in parts:
                    midx = parts.index("metric")
                    metric = parts[midx + 1] if midx + 1 < len(parts) else ""
                return {"interface": iface, "type": itype, "metric": metric}
    except Exception:
        pass
    return {"interface": "unknown", "type": "unknown", "metric": ""}


def _uplink_history_sampler_loop():
    """Sample active uplink every 60s and append changes to history."""
    import time as _time
    last_iface = None
    while True:
        try:
            current = _get_current_uplink()
            iface = current["interface"]
            if iface != last_iface:
                # Record the transition
                entry = dict(current)
                entry["timestamp"] = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
                with _uplink_history_lock:
                    try:
                        text = Path(UPLINK_HISTORY_FILE).read_text().strip()
                        history = json.loads(text) if text else []
                    except (OSError, json.JSONDecodeError):
                        history = []
                    history.append(entry)
                    history = history[-200:]  # Keep last 200 transitions
                    d = str(Path(UPLINK_HISTORY_FILE).parent)
                    fd, tmp = tempfile.mkstemp(dir=d)
                    try:
                        with os.fdopen(fd, "w") as fh:
                            json.dump(history, fh)
                        os.replace(tmp, UPLINK_HISTORY_FILE)
                    except Exception:
                        import contextlib
                        with contextlib.suppress(OSError):
                            os.unlink(tmp)
                last_iface = iface
        except Exception:
            pass
        _time.sleep(60)


# Start uplink history sampler
threading.Thread(target=_uplink_history_sampler_loop, daemon=True).start()


@app.route("/api/uplink/history", methods=["GET"])
@require_auth
def api_uplink_history():
    """Return uplink transition history (newest first)."""
    limit = min(int(request.args.get("limit", 50)), 200)
    with _uplink_history_lock:
        try:
            text = Path(UPLINK_HISTORY_FILE).read_text().strip()
            history = json.loads(text) if text else []
        except (OSError, json.JSONDecodeError):
            history = []
    return jsonify({"history": list(reversed(history))[:limit], "total": len(history)})


# ── Uplink WiFi scan ──────────────────────────────────────────────────────────


@app.route("/api/uplink/scan", methods=["GET"])
@require_auth
def api_uplink_scan():
    # Get current SSID
    current_ssid = None
    link_out, _ = _run(["iw", "dev", "wlan0", "link"])
    for line in link_out.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            current_ssid = line[5:].strip()
            break

    # Run scan
    scan_out, rc = _run(["iw", "dev", "wlan0", "scan"], timeout=20)
    if rc != 0 and not scan_out:
        return jsonify({"networks": [], "current_ssid": current_ssid,
                        "error": "Scan failed (permission or interface error)"})

    networks = []
    current: dict = {}

    for raw_line in scan_out.splitlines():
        line = raw_line.strip()

        # New BSS block — save previous if it has an SSID
        if line.startswith("BSS "):
            if current.get("ssid"):
                networks.append(current)
            bssid_match = re.match(r"BSS ([0-9a-f:]{17})", line)
            current = {
                "ssid": "",
                "bssid": bssid_match.group(1) if bssid_match else "",
                "signal_dbm": -100,
                "security": "Open",
                "channel": 0,
            }
            continue

        if line.startswith("SSID:"):
            ssid = line[5:].strip()
            if ssid:
                current["ssid"] = ssid

        elif line.startswith("signal:"):
            # e.g. "signal: -65.00 dBm"
            m = re.search(r"(-?\d+(?:\.\d+)?)", line)
            if m:
                current["signal_dbm"] = int(float(m.group(1)))

        elif line.startswith("* primary channel:"):
            m = re.search(r"(\d+)", line)
            if m:
                current["channel"] = int(m.group(1))

        elif line.startswith("RSN:"):
            current["security"] = "WPA2"

        elif line.startswith("WPA:") and current.get("security") != "WPA2":
            current["security"] = "WPA"

    # Don't forget last block
    if current.get("ssid"):
        networks.append(current)

    # Sort by signal descending, cap at 20
    networks.sort(key=lambda n: n["signal_dbm"], reverse=True)
    networks = networks[:20]

    return jsonify({"networks": networks, "current_ssid": current_ssid})


# ── Uplink WiFi connect ───────────────────────────────────────────────────────


WPA_SUPPLICANT_CONF = "/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
WPA_SUPPLICANT_HEADER = (
    "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
    "update_config=1\n"
    "country=US\n"
)


@app.route("/api/uplink/connect", methods=["POST"])
@require_auth_always
def api_uplink_connect():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    ssid = data.get("ssid", "")
    if not isinstance(ssid, str) or not ssid:
        return jsonify({"error": "Missing or invalid 'ssid'"}), 400
    if len(ssid) > 64:
        return jsonify({"error": "'ssid' too long (max 64 chars)"}), 400
    if "\x00" in ssid:
        return jsonify({"error": "Invalid characters in 'ssid'"}), 400

    password = data.get("password") or None
    if password is not None:
        if not isinstance(password, str):
            return jsonify({"error": "Invalid 'password'"}), 400
        if len(password) > 64:
            return jsonify({"error": "'password' too long (max 64 chars)"}), 400
        if "\x00" in password:
            return jsonify({"error": "Invalid characters in 'password'"}), 400

    # Build network block
    if password:
        try:
            result = subprocess.run(
                ["wpa_passphrase", ssid, password],
                capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            return jsonify({"error": "wpa_passphrase not found"}), 503
        except subprocess.TimeoutExpired:
            return jsonify({"error": "wpa_passphrase timed out"}), 504
        if result.returncode != 0:
            return jsonify({"error": "wpa_passphrase failed: " + (result.stderr or "unknown")}), 500
        network_block = result.stdout
    else:
        network_block = f'network={{\n\tssid="{ssid}"\n\tkey_mgmt=NONE\n}}\n'

    conf_content = WPA_SUPPLICANT_HEADER + "\n" + network_block

    # Write atomically
    conf_path = Path(WPA_SUPPLICANT_CONF)
    try:
        dir_ = str(conf_path.parent)
        fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".wpa_tmp_")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(conf_content)
            os.replace(tmp_path, str(conf_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except PermissionError:
        return jsonify({"error": "Permission denied writing wpa_supplicant config"}), 503
    except OSError as exc:
        return jsonify({"error": f"Failed to write config: {exc}"}), 500

    # Reconfigure
    _run(["wpa_cli", "-i", "wlan0", "reconfigure"], timeout=10)

    _push_event("uplink_connect", {"ssid": ssid})

    return jsonify({"ok": True, "ssid": ssid})


# ── AP info & channel selector ────────────────────────────────────────────────


@app.route("/api/wifi/ap-config", methods=["GET"])
@require_auth
def api_wifi_ap_config_get():
    """Return current AP configuration from hostapd."""
    cfg = {"ssid": "", "channel": "", "hw_mode": "", "country_code": "", "tx_power": "", "interface": "uap0"}
    try:
        conf_path = Path("/etc/raspap/hostapd.ini")
        if not conf_path.exists():
            conf_path = Path("/etc/hostapd/hostapd.conf")
        text = conf_path.read_text()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("ssid="):
                cfg["ssid"] = line.split("=", 1)[1].strip()
            elif line.startswith("channel="):
                cfg["channel"] = line.split("=", 1)[1].strip()
            elif line.startswith("hw_mode="):
                cfg["hw_mode"] = line.split("=", 1)[1].strip()
            elif line.startswith("country_code="):
                cfg["country_code"] = line.split("=", 1)[1].strip()
            elif line.startswith("interface="):
                cfg["interface"] = line.split("=", 1)[1].strip()
    except OSError:
        pass
    # Get TX power from iw
    try:
        out, _ = _run(["iw", "dev", cfg["interface"], "info"], timeout=3)
        for line in (out or "").splitlines():
            if "txpower" in line.lower():
                cfg["tx_power"] = line.strip()
                break
    except Exception:
        pass
    return jsonify(cfg)


@app.route("/api/wifi/ap-config", methods=["POST"])
@require_auth_always
def api_wifi_ap_config_post():
    """Change AP channel — writes hostapd config and restarts hostapd."""
    body = request.get_json(silent=True) or {}
    channel = str(body.get("channel", "")).strip()
    # Validate: must be 0 (auto) or 1-13 (2.4GHz) or 36-165 (5GHz)
    valid_channels = {"0"} | {str(i) for i in range(1, 14)} | {str(i) for i in range(36, 166, 4)}
    if channel not in valid_channels:
        return jsonify({"error": f"invalid channel: {channel}"}), 400
    try:
        conf_path = Path("/etc/raspap/hostapd.ini")
        if not conf_path.exists():
            conf_path = Path("/etc/hostapd/hostapd.conf")
        text = conf_path.read_text()
        new_lines = []
        found = False
        for line in text.splitlines():
            if line.strip().startswith("channel="):
                new_lines.append(f"channel={channel}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"channel={channel}")
        new_text = "\n".join(new_lines) + "\n"
        # Atomic write
        d = str(conf_path.parent)
        fd, tmp = tempfile.mkstemp(dir=d)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(new_text)
            os.replace(tmp, str(conf_path))
        except Exception:
            import contextlib
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        # Restart hostapd to apply
        _run(["systemctl", "restart", "hostapd"], timeout=10)
        return jsonify({"ok": True, "channel": channel})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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


# ── Bandwidth quota status ────────────────────────────────────────────────────

@app.route("/api/datacap/status", methods=["GET"])
@require_auth
def api_datacap_status():
    """Return current month bandwidth usage vs configured data cap."""
    # Read cap config
    cap_mb = 0
    alert_pct = 80
    try:
        text = Path(DATACAP_FILE).read_text().strip()
        cfg = json.loads(text) if text else {}
        cap_mb = int(cfg.get("cap_mb", 0))
        alert_pct = int(cfg.get("alert_pct", 80))
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    if cap_mb <= 0:
        return jsonify({"enabled": False, "cap_mb": 0, "used_mb": 0, "pct": 0, "alert_pct": alert_pct})
    # Read current month usage from vnstat
    used_mb = 0
    try:
        out, _ = _run(["vnstat", "--json", "m", "1"], timeout=5)
        if out:
            data = json.loads(out)
            # vnstat JSON: interfaces[0].months[-1].{rx,tx} in bytes or KiB depending on version
            for iface_data in (data.get("interfaces") or []):
                months = iface_data.get("traffic", {}).get("months") or iface_data.get("months") or []
                if months:
                    m = months[-1]
                    # Try bytes first (vnstat 2.x), then KiB
                    rx = m.get("rx", 0)
                    tx = m.get("tx", 0)
                    if rx > 0 or tx > 0:
                        # If values seem too small to be bytes, assume KiB
                        total = rx + tx
                        if total < 1_000_000:  # likely KiB
                            used_mb += total / 1024
                        else:  # bytes
                            used_mb += total / (1024 * 1024)
    except Exception:
        pass
    used_mb = round(used_mb, 1)
    pct = round((used_mb / cap_mb) * 100, 1) if cap_mb > 0 else 0
    over_alert = pct >= alert_pct
    over_cap = used_mb >= cap_mb
    return jsonify({
        "enabled": True,
        "cap_mb": cap_mb,
        "cap_gb": round(cap_mb / 1024, 2),
        "used_mb": used_mb,
        "used_gb": round(used_mb / 1024, 2),
        "pct": pct,
        "alert_pct": alert_pct,
        "over_alert": over_alert,
        "over_cap": over_cap,
    })


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


# ── Firewall rules endpoint ───────────────────────────────────────────────────

_RAW_CAP = 4096


def _parse_iptables_output(raw: str) -> list:
    """Parse iptables -L -n -v --line-numbers output into chain dicts.

    Returns a list of:
        {"chain": str, "policy": str|None, "rules": [{"num", "target", "prot",
         "source", "destination", "options", "pkts", "bytes"}, ...]}
    """
    chains = []
    current: dict | None = None

    for line in raw.splitlines():
        # New chain header: "Chain INPUT (policy ACCEPT)" or "Chain FORWARD (2 references)"
        m = re.match(r"^Chain\s+(\S+)\s+\((.+)\)", line)
        if m:
            if current is not None:
                chains.append(current)
            chain_name = m.group(1)
            policy_str = m.group(2)
            policy_m = re.search(r"policy\s+(\S+)", policy_str)
            policy = policy_m.group(1) if policy_m else None
            current = {"chain": chain_name, "policy": policy, "rules": []}
            continue

        if current is None:
            continue

        # Skip column header lines
        if re.match(r"^\s*(pkts|num)\s", line):
            continue

        stripped = line.strip()
        if not stripped:
            continue

        # Parse rule line: num pkts bytes target prot opt in out source destination [options...]
        # e.g.: "1  12345  1234567  ACCEPT  all  --  *  *  0.0.0.0/0  0.0.0.0/0  state RELATED,ESTABLISHED"
        parts = stripped.split(None, 10)
        if len(parts) < 9:
            continue

        # Determine if first field is a line number (digit) or pkts counter
        try:
            int(parts[0])
        except ValueError:
            continue  # skip non-rule lines

        # With --line-numbers: num pkts bytes target prot opt in out source destination [options]
        # Without: pkts bytes target prot opt in out source destination [options]
        # We always use --line-numbers so we have 10+ fields
        if len(parts) >= 10:
            rule: dict = {
                "num": parts[0],
                "pkts": parts[1],
                "bytes": parts[2],
                "target": parts[3],
                "prot": parts[4],
                "source": parts[8],
                "destination": parts[9],
                "options": parts[10].strip() if len(parts) > 10 else "",
            }
            current["rules"].append(rule)

    if current is not None:
        chains.append(current)

    return chains


@app.route("/api/firewall/rules")
@require_auth
def api_firewall_rules():
    """Return current iptables rules for filter and nat tables."""
    raw_filter = ""
    raw_nat = ""
    error = None

    try:
        out_filter, rc_filter = _run(
            ["iptables", "-L", "-n", "-v", "--line-numbers"], timeout=10
        )
        raw_filter = (out_filter or "")[:_RAW_CAP]
    except FileNotFoundError:
        error = "iptables not available on this host"
    except Exception as exc:
        error = str(exc)

    if error is None:
        try:
            out_nat, rc_nat = _run(
                ["iptables", "-t", "nat", "-L", "-n", "-v", "--line-numbers"], timeout=10
            )
            raw_nat = (out_nat or "")[:_RAW_CAP]
        except Exception:
            raw_nat = ""

    filter_chains = _parse_iptables_output(raw_filter) if raw_filter else []
    nat_chains = _parse_iptables_output(raw_nat) if raw_nat else []

    result: dict = {
        "tables": {
            "filter": filter_chains,
            "nat": nat_chains,
        },
        "raw_filter": raw_filter,
        "raw_nat": raw_nat,
    }
    if error:
        result["error"] = error

    return jsonify(result)


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


# ── System resource history endpoint ─────────────────────────────────────────


@app.route("/api/system/resources", methods=["GET"])
@require_auth
def api_system_resources():
    """Return last 60 resource samples, the most recent snapshot, plus uptime/load/mem details."""
    with _RESOURCE_LOCK:
        history = list(_RESOURCE_HISTORY[-60:])
    current = history[-1] if history else None

    # Enrich with fields not stored in the background sampler
    # Memory details (MB)
    mem_total_mb = mem_used_mb = mem_avail_mb = mem_pct_detail = None
    try:
        meminfo: dict = {}
        for _line in Path("/proc/meminfo").read_text().splitlines():
            _k, _, _v = _line.partition(":")
            meminfo[_k.strip()] = int(_v.split()[0])
        _mt = meminfo.get("MemTotal", 0)
        _ma = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        if _mt > 0:
            mem_total_mb = round(_mt / 1024, 1)
            mem_avail_mb = round(_ma / 1024, 1)
            mem_used_mb = round(mem_total_mb - mem_avail_mb, 1)
            mem_pct_detail = round(100.0 * mem_used_mb / mem_total_mb, 1)
    except Exception:
        pass

    # Uptime
    uptime_seconds = None
    uptime_human = None
    try:
        _secs = float(Path("/proc/uptime").read_text().split()[0])
        uptime_seconds = int(_secs)
        _d = uptime_seconds // 86400
        _h = (uptime_seconds % 86400) // 3600
        _m = (uptime_seconds % 3600) // 60
        uptime_human = f"{_d}d {_h}h {_m}m" if _d > 0 else f"{_h}h {_m}m"
    except Exception:
        pass

    # Load averages
    load_1 = load_5 = load_15 = None
    try:
        _parts = Path("/proc/loadavg").read_text().split()
        load_1 = float(_parts[0])
        load_5 = float(_parts[1])
        load_15 = float(_parts[2])
    except Exception:
        pass

    return jsonify({
        "current": current,
        "history": history,
        "mem_total_mb": mem_total_mb,
        "mem_used_mb": mem_used_mb,
        "mem_avail_mb": mem_avail_mb,
        "mem_pct": mem_pct_detail,
        "uptime_seconds": uptime_seconds,
        "uptime_human": uptime_human,
        "load_1": load_1,
        "load_5": load_5,
        "load_15": load_15,
    })


# ── Pi throttling monitor ─────────────────────────────────────────────────────

def _parse_throttled(hex_val: str) -> dict:
    """Parse vcgencmd get_throttled hex value into human-readable flags."""
    try:
        val = int(hex_val, 16)
    except ValueError:
        return {"raw": hex_val, "flags": [], "ok": True}
    flags = []
    # Current flags (bits 0-3)
    if val & 0x1:
        flags.append("undervoltage-detected")
    if val & 0x2:
        flags.append("arm-freq-capped")
    if val & 0x4:
        flags.append("throttled")
    if val & 0x8:
        flags.append("soft-temp-limit")
    # Historical flags (bits 16-19)
    if val & 0x10000:
        flags.append("undervoltage-occurred")
    if val & 0x20000:
        flags.append("arm-freq-capped-occurred")
    if val & 0x40000:
        flags.append("throttling-occurred")
    if val & 0x80000:
        flags.append("soft-temp-limit-occurred")
    current_ok = (val & 0xF) == 0
    return {"raw": hex_val, "value": val, "flags": flags, "ok": current_ok,
            "undervoltage": bool(val & 0x1), "throttled": bool(val & 0x4),
            "freq_capped": bool(val & 0x2), "soft_temp": bool(val & 0x8)}


@app.route("/api/system/throttle", methods=["GET"])
@require_auth
def api_system_throttle():
    """Return Pi throttling status from vcgencmd get_throttled."""
    throttle_info = {"available": False, "raw": None, "ok": True, "flags": []}
    try:
        out, _ = _run(["vcgencmd", "get_throttled"], timeout=3)
        if out and "throttled=" in out:
            hex_val = out.strip().split("=", 1)[1].strip()
            throttle_info = _parse_throttled(hex_val)
            throttle_info["available"] = True
    except Exception:
        pass
    # Current CPU temp (already in system resources, but useful inline)
    temp_c = None
    try:
        temp_str = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        temp_c = round(int(temp_str) / 1000, 1)
    except (OSError, ValueError):
        pass
    throttle_info["temp_c"] = temp_c
    # CPU frequency
    freq_mhz = None
    try:
        freq_str = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").read_text().strip()
        freq_mhz = round(int(freq_str) / 1000)
    except (OSError, ValueError):
        pass
    throttle_info["freq_mhz"] = freq_mhz
    return jsonify(throttle_info)


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


# ── Port forwarding ──────────────────────────────────────────────────────────

_PF_IP_RE = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')


def _read_portforward() -> list:
    """Read port-forward rules from JSON store. Returns [] on any error."""
    try:
        text = Path(PORT_FORWARD_FILE).read_text().strip()
        if not text:
            return []
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _write_portforward(rules: list) -> None:
    """Atomically write port-forward rules list to disk."""
    pf_dir = str(Path(PORT_FORWARD_FILE).parent)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=pf_dir, prefix="portforward.")
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            json.dump(rules, fh)
        os.replace(tmp_path, PORT_FORWARD_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _apply_portforward_rules(rules: list) -> None:
    """Flush and reapply all iptables DNAT rules from the rules list."""
    # Flush existing chains (ignore errors — chains may not exist yet)
    _run(["iptables", "-t", "nat", "-F", "TRAVEL_PORTFWD"])
    _run(["iptables", "-F", "TRAVEL_PORTFWD"])
    # Create chains if not present
    _run(["iptables", "-t", "nat", "-N", "TRAVEL_PORTFWD"])
    _run(["iptables", "-N", "TRAVEL_PORTFWD"])
    # Ensure jump from PREROUTING and FORWARD
    out, rc = _run(["iptables", "-t", "nat", "-C", "PREROUTING", "-j", "TRAVEL_PORTFWD"])
    if rc != 0:
        _run(["iptables", "-t", "nat", "-I", "PREROUTING", "1", "-j", "TRAVEL_PORTFWD"])
    out, rc = _run(["iptables", "-C", "FORWARD", "-j", "TRAVEL_PORTFWD"])
    if rc != 0:
        _run(["iptables", "-I", "FORWARD", "1", "-j", "TRAVEL_PORTFWD"])
    # Add rules
    for rule in rules:
        proto = rule.get("proto", "tcp")
        ext_port = str(rule.get("ext_port", ""))
        int_ip = rule.get("int_ip", "")
        int_port = str(rule.get("int_port", ""))
        _run([
            "iptables", "-t", "nat", "-A", "TRAVEL_PORTFWD",
            "-p", proto, "--dport", ext_port,
            "-j", "DNAT", "--to-destination", f"{int_ip}:{int_port}",
        ])
        _run([
            "iptables", "-A", "TRAVEL_PORTFWD",
            "-p", proto, "-d", int_ip, "--dport", int_port,
            "-j", "ACCEPT",
        ])


@app.route("/api/portforward", methods=["GET"])
@require_auth
def api_portforward_get():
    return jsonify({"rules": _read_portforward()})


@app.route("/api/portforward", methods=["POST"])
@require_auth_always
def api_portforward_post():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    proto = data.get("proto", "")
    if proto not in ("tcp", "udp"):
        return jsonify({"error": "proto must be 'tcp' or 'udp'"}), 400

    ext_port = data.get("ext_port")
    if not isinstance(ext_port, int) or not (1 <= ext_port <= 65535):
        return jsonify({"error": "ext_port must be an integer 1–65535"}), 400

    int_ip = data.get("int_ip", "")
    if not isinstance(int_ip, str) or not _PF_IP_RE.match(int_ip):
        return jsonify({"error": "int_ip must be a valid IPv4 address"}), 400
    # Validate each octet <= 255
    if any(int(o) > 255 for o in int_ip.split(".")):
        return jsonify({"error": "int_ip octet out of range"}), 400

    int_port = data.get("int_port")
    if not isinstance(int_port, int) or not (1 <= int_port <= 65535):
        return jsonify({"error": "int_port must be an integer 1–65535"}), 400

    comment = data.get("comment", "")
    if not isinstance(comment, str):
        comment = ""
    comment = re.sub(r'<[^>]*>', '', comment).strip()
    if len(comment) > 64:
        return jsonify({"error": "comment must be at most 64 characters"}), 400

    rules = _read_portforward()
    # Check for duplicate ext_port + proto
    for r in rules:
        if r.get("proto") == proto and r.get("ext_port") == ext_port:
            return jsonify({"error": f"Rule for {proto}/{ext_port} already exists"}), 409

    rule = {
        "id": uuid4().hex[:8],
        "proto": proto,
        "ext_port": ext_port,
        "int_ip": int_ip,
        "int_port": int_port,
        "comment": comment,
    }
    rules.append(rule)
    try:
        _write_portforward(rules)
    except Exception as exc:
        return jsonify({"error": f"Failed to save rules: {exc}"}), 503
    _apply_portforward_rules(rules)
    return jsonify({"ok": True, "rule": rule})


@app.route("/api/portforward/<rule_id>", methods=["DELETE"])
@require_auth_always
def api_portforward_delete(rule_id):
    rules = _read_portforward()
    new_rules = [r for r in rules if r.get("id") != rule_id]
    if len(new_rules) == len(rules):
        return jsonify({"error": "Rule not found"}), 404
    try:
        _write_portforward(new_rules)
    except Exception as exc:
        return jsonify({"error": f"Failed to save rules: {exc}"}), 503
    _apply_portforward_rules(new_rules)
    return jsonify({"ok": True})


# ── Client connection history ─────────────────────────────────────────────────

CLIENT_HISTORY_FILE = "/var/lib/travel-router/client-history.json"
_DHCP_LEASES_FILE = "/var/lib/misc/dnsmasq.leases"
_CLIENT_HISTORY_MAX = 200  # max events to keep
_client_history_lock = threading.Lock()


def _read_client_history() -> list:
    try:
        text = Path(CLIENT_HISTORY_FILE).read_text().strip()
        if not text:
            return []
        return json.loads(text)
    except (OSError, json.JSONDecodeError):
        return []


def _append_client_event(mac: str, ip: str, hostname: str, event: str) -> None:
    """Append a connect/disconnect event to client history."""
    with _client_history_lock:
        history = _read_client_history()
        history.append({
            "ts": int(time.time()),
            "mac": mac.lower(),
            "ip": ip,
            "hostname": hostname or "",
            "event": event,  # "connect" or "disconnect"
        })
        # Keep only the most recent N events
        if len(history) > _CLIENT_HISTORY_MAX:
            history = history[-_CLIENT_HISTORY_MAX:]
        try:
            d = str(Path(CLIENT_HISTORY_FILE).parent)
            fd, tmp = tempfile.mkstemp(dir=d)
            with os.fdopen(fd, "w") as fh:
                json.dump(history, fh)
            os.replace(tmp, CLIENT_HISTORY_FILE)
        except Exception:
            pass


_LAST_LEASES: dict = {}  # mac -> {ip, hostname, expiry}


def _leases_sampler_loop() -> None:
    global _LAST_LEASES
    while True:
        time.sleep(30)
        try:
            text = Path(_DHCP_LEASES_FILE).read_text()
        except OSError:
            continue
        current: dict = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            expiry, mac, ip, hostname = parts[0], parts[1], parts[2], parts[3]
            current[mac.lower()] = {"ip": ip, "hostname": hostname, "expiry": expiry}
        # Detect new connections
        for mac, info in current.items():
            if mac not in _LAST_LEASES:
                _append_client_event(mac, info["ip"], info["hostname"], "connect")
        # Detect disconnections (lease expired/removed)
        for mac, info in _LAST_LEASES.items():
            if mac not in current:
                _append_client_event(mac, info["ip"], info["hostname"], "disconnect")
        _LAST_LEASES = current


threading.Thread(target=_leases_sampler_loop, daemon=True).start()


@app.route("/api/clients/history", methods=["GET"])
@require_auth
def api_clients_history():
    """Return recent client connection/disconnection events."""
    limit = min(int(request.args.get("limit", 50)), 200)
    history = _read_client_history()
    # Return most recent events first
    return jsonify({"events": list(reversed(history[-limit:]))})


# ── DHCP Leases ───────────────────────────────────────────────────────────────

@app.route("/api/network/dhcp/leases", methods=["GET"])
@require_auth
def api_network_dhcp_leases():
    """Return current DHCP leases from dnsmasq leases file."""
    import time
    leases_paths = [
        "/var/lib/misc/dnsmasq.leases",
        "/var/lib/dnsmasq/dnsmasq.leases",
        "/tmp/dnsmasq.leases",
    ]
    leases = []
    leases_file = None
    for path in leases_paths:
        if os.path.exists(path):
            leases_file = path
            break

    if leases_file:
        try:
            with open(leases_file) as f:
                now = int(time.time())
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    try:
                        expire_ts = int(parts[0])
                    except ValueError:
                        continue
                    mac = parts[1]
                    ip = parts[2]
                    hostname = parts[3] if parts[3] != "*" else None
                    client_id = parts[4] if len(parts) > 4 and parts[4] != "*" else None
                    if expire_ts == 0:
                        ttl_label = "static"
                        expires_in = None
                    else:
                        ttl = expire_ts - now
                        expires_in = ttl
                        if ttl <= 0:
                            ttl_label = "expired"
                        elif ttl < 60:
                            ttl_label = f"{ttl}s"
                        elif ttl < 3600:
                            ttl_label = f"{ttl // 60}m"
                        else:
                            ttl_label = f"{ttl // 3600}h {(ttl % 3600) // 60}m"
                    leases.append({
                        "mac": mac,
                        "ip": ip,
                        "hostname": hostname,
                        "client_id": client_id,
                        "expire_ts": expire_ts,
                        "expires_in": expires_in,
                        "ttl_label": ttl_label,
                    })
        except OSError as e:
            return jsonify({"error": str(e), "leases": [], "count": 0, "leases_file": leases_file})

    leases.sort(key=lambda x: x["ip"])
    return jsonify({
        "leases": leases,
        "count": len(leases),
        "leases_file": leases_file,
    })


# ── Firewall Rules ────────────────────────────────────────────────────────────

@app.route("/api/network/firewall", methods=["GET"])
@require_auth
def api_network_firewall():
    """Return firewall rules: nft list ruleset (preferred) or iptables -L."""
    import re

    # Try nftables first
    out, rc = _run(["nft", "-j", "list", "ruleset"])
    if rc == 0:
        import json as _json
        try:
            data = _json.loads(out)
            rules = []
            for item in data.get("nftables", []):
                if "rule" in item:
                    r = item["rule"]
                    table = r.get("table", "")
                    chain = r.get("chain", "")
                    expr_list = r.get("expr", [])
                    # Stringify expr for display
                    parts = []
                    for expr in expr_list:
                        if "match" in expr:
                            m = expr["match"]
                            left = str(m.get("left", {}).get("payload", {}).get("field", ""))
                            right = str(m.get("right", ""))
                            parts.append(f"{left}={right}")
                        elif "accept" in expr:
                            parts.append("ACCEPT")
                        elif "drop" in expr:
                            parts.append("DROP")
                        elif "reject" in expr:
                            parts.append("REJECT")
                        elif "counter" in expr:
                            c = expr["counter"]
                            parts.append(f"pkts={c.get('packets',0)} bytes={c.get('bytes',0)}")
                    rules.append({
                        "table": table,
                        "chain": chain,
                        "rule": " ".join(parts) if parts else str(expr_list),
                        "tool": "nft",
                    })
            return jsonify({"tool": "nft", "rules": rules, "count": len(rules)})
        except (ValueError, KeyError):
            pass

    # Try nft plain text as fallback
    out, rc = _run(["nft", "list", "ruleset"])
    if rc == 0:
        lines = [l for l in out.splitlines() if l.strip() and not l.startswith("#")]
        rules = []
        current_table = ""
        current_chain = ""
        for line in lines:
            line_s = line.strip()
            m = re.match(r'^table (\S+ \S+)', line_s)
            if m:
                current_table = m.group(1)
                continue
            m = re.match(r'^chain (\S+)', line_s)
            if m:
                current_chain = m.group(1)
                continue
            if line_s and line_s not in ('{', '}'):
                rules.append({"table": current_table, "chain": current_chain, "rule": line_s, "tool": "nft"})
        return jsonify({"tool": "nft", "rules": rules, "count": len(rules)})

    # Fall back to iptables
    chains = []
    for table in ("filter", "nat", "mangle"):
        out, rc = _run(["iptables", "-t", table, "-L", "-n", "-v", "--line-numbers"])
        if rc != 0:
            continue
        current_chain = ""
        for line in out.splitlines():
            m = re.match(r'^Chain (\S+)', line)
            if m:
                current_chain = m.group(1)
                continue
            # Rule lines start with a line number
            m = re.match(r'^\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.*)', line)
            if m:
                chains.append({
                    "table": table,
                    "chain": current_chain,
                    "rule": line.strip(),
                    "tool": "iptables",
                })
    if chains:
        return jsonify({"tool": "iptables", "rules": chains, "count": len(chains)})

    return jsonify({"tool": "none", "rules": [], "count": 0, "error": "No firewall tool available (nft/iptables)"})


# ── Bandwidth History ─────────────────────────────────────────────────────────

@app.route("/api/network/bandwidth", methods=["GET"])
@require_auth
def api_network_bandwidth():
    """Return bandwidth history from vnstat for the last 24 hours (hourly) and 30 days (daily)."""
    import json as _json

    result = {"hourly": [], "daily": [], "interface": None, "available": False}

    out, rc = _run(["vnstat", "--json"])
    if rc != 0:
        result["error"] = "vnstat not available or no data yet"
        return jsonify(result)

    try:
        data = _json.loads(out)
    except ValueError:
        result["error"] = "Could not parse vnstat output"
        return jsonify(result)

    interfaces = data.get("interfaces", [])
    if not interfaces:
        result["error"] = "No interfaces tracked by vnstat"
        return jsonify(result)

    iface = None
    for i in interfaces:
        if i.get("name") not in ("lo",):
            iface = i
            break
    if not iface:
        iface = interfaces[0]

    result["interface"] = iface.get("name")
    result["available"] = True

    hourly = iface.get("traffic", {}).get("hour", [])
    hourly_out = []
    for h in hourly[-24:]:
        ts = h.get("date", {})
        t = h.get("time", {})
        label = f"{ts.get('year',0)}-{str(ts.get('month',0)).zfill(2)}-{str(ts.get('day',0)).zfill(2)} {str(t.get('hour',0)).zfill(2)}:00"
        hourly_out.append({
            "label": label,
            "rx_mb": round(h.get("rx", 0) / 1024 / 1024, 2),
            "tx_mb": round(h.get("tx", 0) / 1024 / 1024, 2),
        })
    result["hourly"] = hourly_out

    daily = iface.get("traffic", {}).get("day", [])
    daily_out = []
    for d in daily[-30:]:
        ts = d.get("date", {})
        label = f"{ts.get('year',0)}-{str(ts.get('month',0)).zfill(2)}-{str(ts.get('day',0)).zfill(2)}"
        daily_out.append({
            "label": label,
            "rx_gb": round(d.get("rx", 0) / 1024 / 1024 / 1024, 3),
            "tx_gb": round(d.get("tx", 0) / 1024 / 1024 / 1024, 3),
        })
    result["daily"] = daily_out

    return jsonify(result)


# ── DNS Resolver Config ───────────────────────────────────────────────────────

@app.route("/api/dns/resolvers", methods=["GET"])
@require_auth
def api_dns_resolvers():
    """Return current DNS resolver configuration and test resolution latency."""
    import time as _time

    nameservers = []
    search_domains = []
    options = []
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                line = line.strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2:
                        nameservers.append(parts[1])
                elif line.startswith("search"):
                    search_domains = line.split()[1:]
                elif line.startswith("options"):
                    options = line.split()[1:]
    except OSError:
        pass

    resolver_results = []
    for ns in nameservers[:4]:
        start = _time.monotonic()
        out, rc = _run(["dig", "+short", "+time=2", "+tries=1", f"@{ns}", "google.com", "A"])
        elapsed_ms = round((_time.monotonic() - start) * 1000)
        resolved = rc == 0 and out.strip() != ""
        resolver_results.append({
            "address": ns,
            "reachable": resolved,
            "latency_ms": elapsed_ms if resolved else None,
            "response": out.strip().splitlines()[0] if resolved and out.strip() else None,
        })

    doh_active = False
    doh_provider = None
    out2, rc2 = _run(["grep", "-r", "cloudflare-dns\\|dns.google\\|quad9", "/etc/dnsmasq.d/"])
    if rc2 == 0 and out2.strip():
        doh_active = True
        if "cloudflare" in out2.lower():
            doh_provider = "Cloudflare"
        elif "google" in out2.lower():
            doh_provider = "Google"
        elif "quad9" in out2.lower():
            doh_provider = "Quad9"

    return jsonify({
        "nameservers": resolver_results,
        "search_domains": search_domains,
        "options": options,
        "doh_active": doh_active,
        "doh_provider": doh_provider,
        "count": len(resolver_results),
    })


# ── SSH Authorized Keys ───────────────────────────────────────────────────────

@app.route("/api/system/ssh/keys", methods=["GET"])
@require_auth
def api_system_ssh_keys():
    """Return parsed SSH authorized_keys entries for root and pi/travel-router users."""
    import re
    import os

    users_to_check = ["root", "pi", "travel-router"]
    all_keys = []

    for user in users_to_check:
        path = "/root/.ssh/authorized_keys" if user == "root" else f"/home/{user}/.ssh/authorized_keys"
        try:
            with open(path) as f:
                content = f.read()
        except OSError:
            continue
        key_types = {
            "ssh-rsa", "ssh-ed25519", "ssh-dss", "ecdsa-sha2-nistp256",
            "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com",
        }
        for lineno, line in enumerate(content.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            options = ""
            keytype = ""
            pubkey = ""
            comment = ""
            if parts[0] in key_types:
                keytype, pubkey = parts[0], parts[1]
                comment = " ".join(parts[2:]) if len(parts) > 2 else ""
            else:
                options = parts[0]
                if len(parts) >= 3 and parts[1] in key_types:
                    keytype, pubkey = parts[1], parts[2]
                    comment = " ".join(parts[3:]) if len(parts) > 3 else ""
                else:
                    keytype = parts[1] if len(parts) > 1 else ""
                    pubkey = parts[2] if len(parts) > 2 else ""
                    comment = " ".join(parts[3:]) if len(parts) > 3 else ""
            fingerprint = pubkey[-16:] if len(pubkey) > 16 else pubkey
            all_keys.append({
                "user": user,
                "file": path,
                "line": lineno,
                "type": keytype,
                "comment": comment,
                "fingerprint": f"…{fingerprint}",
                "options": options,
            })

    return jsonify({"keys": all_keys, "count": len(all_keys)})


# ── Route Table ───────────────────────────────────────────────────────────────

@app.route("/api/network/routes", methods=["GET"])
@require_auth
def api_network_routes():
    """Return IPv4 and IPv6 routing table."""
    routes = []

    # IPv4 routes
    out4, rc4 = _run(["ip", "-4", "route", "show"])
    if rc4 == 0:
        for line in out4.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            route = {"family": "4", "dest": parts[0], "gateway": None, "dev": None, "metric": None, "proto": None, "raw": line}
            for i, p in enumerate(parts):
                if p == "via" and i + 1 < len(parts):
                    route["gateway"] = parts[i + 1]
                elif p == "dev" and i + 1 < len(parts):
                    route["dev"] = parts[i + 1]
                elif p == "metric" and i + 1 < len(parts):
                    route["metric"] = parts[i + 1]
                elif p == "proto" and i + 1 < len(parts):
                    route["proto"] = parts[i + 1]
            routes.append(route)

    # IPv6 routes (skip link-local and loopback)
    out6, rc6 = _run(["ip", "-6", "route", "show"])
    if rc6 == 0:
        for line in out6.splitlines():
            line = line.strip()
            if not line or line.startswith("fe80") or line.startswith("::1"):
                continue
            parts = line.split()
            route = {"family": "6", "dest": parts[0], "gateway": None, "dev": None, "metric": None, "proto": None, "raw": line}
            for i, p in enumerate(parts):
                if p == "via" and i + 1 < len(parts):
                    route["gateway"] = parts[i + 1]
                elif p == "dev" and i + 1 < len(parts):
                    route["dev"] = parts[i + 1]
                elif p == "metric" and i + 1 < len(parts):
                    route["metric"] = parts[i + 1]
                elif p == "proto" and i + 1 < len(parts):
                    route["proto"] = parts[i + 1]
            routes.append(route)

    return jsonify({"routes": routes, "count": len(routes)})


# ── Active Connections ────────────────────────────────────────────────────────

@app.route("/api/network/connections", methods=["GET"])
@require_auth
def api_network_connections():
    """Return active TCP/UDP connections from ss."""
    import re

    connections = []

    # ss -tunatp: TCP+UDP, numeric, all states, with process info
    out, rc = _run(["ss", "-tunatp"])
    if rc != 0:
        return jsonify({"error": "ss not available", "connections": []})

    for line in out.splitlines()[1:]:  # skip header
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue

        proto = parts[0]
        state = parts[1] if proto == "tcp" or proto.startswith("tcp") else "—"
        local = parts[4] if len(parts) > 4 else ""
        peer = parts[5] if len(parts) > 5 else ""
        process = parts[6] if len(parts) > 6 else ""

        # Skip LISTEN on loopback and TIME-WAIT/CLOSE-WAIT clutter
        if state in ("TIME-WAIT", "CLOSE-WAIT"):
            continue

        # Extract process name from ss output like users:(("sshd",pid=1234,fd=3))
        proc_name = None
        m = re.search(r'users:\(\("([^"]+)"', process)
        if m:
            proc_name = m.group(1)

        # Skip pure loopback connections (both sides 127.x or ::1)
        if (local.startswith("127.") or local.startswith("[::1]")) and \
           (peer.startswith("127.") or peer.startswith("[::1]") or peer == "*"):
            continue

        connections.append({
            "proto": proto,
            "state": state,
            "local": local,
            "peer": peer,
            "process": proc_name,
        })

    # Sort: ESTABLISHED first, then by proto
    state_order = {"ESTABLISHED": 0, "LISTEN": 1, "SYN-SENT": 2, "SYN-RECV": 3}
    connections.sort(key=lambda c: (state_order.get(c["state"], 9), c["proto"], c["local"]))

    return jsonify({"connections": connections, "count": len(connections)})


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


def _append_speedtest_result(result: dict) -> None:
    """Append a speedtest result to the history file (max 50 entries)."""
    try:
        try:
            text = Path(SPEEDTEST_HISTORY_FILE).read_text().strip()
            history = json.loads(text) if text else []
        except (OSError, json.JSONDecodeError):
            history = []
        import time as _time
        result["timestamp"] = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        history.append(result)
        history = history[-50:]  # Keep last 50
        d = str(Path(SPEEDTEST_HISTORY_FILE).parent)
        fd, tmp = tempfile.mkstemp(dir=d)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(history, fh)
            os.replace(tmp, SPEEDTEST_HISTORY_FILE)
        except Exception:
            import contextlib
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    except Exception:
        pass  # Best-effort — don't break speedtest if history write fails


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

    _append_speedtest_result({
        "download_mbps": data.get("download_mbps"),
        "upload_mbps": data.get("upload_mbps"),
        "ping_ms": data.get("ping_ms"),
        "server": data.get("server"),
        "method": data.get("method"),
    })

    return jsonify({
        "ok": True,
        "cached": False,
        "download_mbps": data.get("download_mbps"),
        "upload_mbps": data.get("upload_mbps"),
        "ping_ms": data.get("ping_ms"),
        "server": data.get("server"),
        "method": data.get("method"),
    })


@app.route("/api/system/speedtest/history", methods=["GET"])
@require_auth
def api_speedtest_history():
    """Return speedtest history (newest first)."""
    try:
        text = Path(SPEEDTEST_HISTORY_FILE).read_text().strip()
        history = json.loads(text) if text else []
    except (OSError, json.JSONDecodeError):
        history = []
    return jsonify({"history": list(reversed(history)), "count": len(history)})


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


# ── Update checker ────────────────────────────────────────────────────────────

_UPDATE_CACHE: dict = {"ts": 0, "data": {}}
_UPDATE_LOCK = threading.Lock()
_GITHUB_RELEASES_URL = "https://api.github.com/repos/NicoMancinelli/pi-travel-router/releases/latest"


def _get_installed_version() -> str:
    try:
        return Path("/etc/travel-router-version").read_text().strip()
    except OSError:
        # Fall back to local VERSION file for dev
        try:
            return Path("VERSION").read_text().strip()
        except OSError:
            return "unknown"


@app.route("/api/update/check", methods=["GET"])
@require_auth
def api_update_check():
    """Check GitHub releases for a newer version. Cached for 1 hour."""
    import urllib.request

    with _UPDATE_LOCK:
        now = time.time()
        if now - _UPDATE_CACHE["ts"] < 3600 and _UPDATE_CACHE["data"]:
            return jsonify(_UPDATE_CACHE["data"])

    installed = _get_installed_version()
    result: dict = {
        "installed": installed,
        "latest": None,
        "update_available": False,
        "release_url": None,
        "error": None,
        "cached": False,
    }

    try:
        req = urllib.request.Request(
            _GITHUB_RELEASES_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "travel-router/1"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            gh = json.loads(resp.read())
        latest = gh.get("tag_name", "").lstrip("v")
        result["latest"] = latest
        result["release_url"] = gh.get("html_url", "")

        def _vt(s):
            try:
                return tuple(int(x) for x in s.split("."))
            except ValueError:
                return (0,)

        if latest and installed != "unknown":
            result["update_available"] = _vt(latest) > _vt(installed)
    except Exception as exc:
        result["error"] = str(exc)[:128]

    with _UPDATE_LOCK:
        _UPDATE_CACHE["ts"] = time.time()
        _UPDATE_CACHE["data"] = {**result, "cached": True}

    return jsonify(result)


@app.route("/api/update/apply", methods=["POST"])
@require_auth_always
def api_update_apply():
    """Trigger the update-router.sh script in the background."""
    script = "/usr/local/sbin/update-router.sh"
    if not Path(script).exists():
        return jsonify({"error": f"{script} not found"}), 404
    import subprocess
    subprocess.Popen(
        ["sudo", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _push_event("update_started", {"msg": "Update started — router will restart when complete"})
    return jsonify({"ok": True, "msg": "Update started in background"})


# ── Tailscale peer map ────────────────────────────────────────────────────────

@app.route("/api/tailscale/peers", methods=["GET"])
@require_auth
def api_tailscale_peers():
    """Return Tailscale peer info from 'tailscale status --json'."""
    try:
        out, rc = _run(["tailscale", "status", "--json"], timeout=10)
    except FileNotFoundError:
        return jsonify({"peers": [], "self": None, "error": "tailscale not available"}), 200

    if not out:
        return jsonify({"peers": [], "self": None, "error": "no output from tailscale"}), 200

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return jsonify({"peers": [], "self": None, "error": "failed to parse tailscale output"}), 200

    def _peer_dict(k, v):
        addrs = v.get("TailscaleIPs") or v.get("Addrs") or []
        last_seen = v.get("LastSeen") or v.get("LastWrite") or ""
        return {
            "id": k[:12],
            "hostname": v.get("HostName") or v.get("DNSName", "").split(".")[0],
            "dns_name": v.get("DNSName", ""),
            "ips": addrs[:2],  # max 2 IPs (IPv4 + IPv6)
            "online": v.get("Online", False),
            "os": v.get("OS", ""),
            "last_seen": last_seen,
            "relay": v.get("Relay", ""),
            "exit_node": v.get("ExitNode", False),
        }

    peers = []
    for node_key, peer in (data.get("Peer") or {}).items():
        peers.append(_peer_dict(node_key, peer))

    # Sort: online first, then alphabetically
    peers.sort(key=lambda p: (not p["online"], p["hostname"].lower()))

    self_info = None
    if data.get("Self"):
        self_info = _peer_dict("self", data["Self"])

    return jsonify({"peers": peers, "self": self_info})


# ── Tailscale exit node control ───────────────────────────────────────────────

@app.route("/api/tailscale/exit-node", methods=["GET"])
@require_auth
def api_tailscale_exit_node_get():
    """Return current exit node and list of available exit node peers."""
    try:
        out, _ = _run(["tailscale", "status", "--json"], timeout=5)
        if not out:
            return jsonify({"error": "tailscale not running", "current": None, "peers": []}), 503
        import json as _json
        data = _json.loads(out)
        # Current exit node
        current_exit = None
        self_node = data.get("Self", {})
        if self_node.get("ExitNodeOption"):
            current_exit = self_node.get("ExitNodeOption")
        # Also check peer list for active exit node
        peer_list = []
        for peer_id, peer in (data.get("Peer") or {}).items():
            if peer.get("ExitNodeOption") or peer.get("ExitNode"):
                name = peer.get("HostName") or peer.get("DNSName", "").split(".")[0]
                ip = (peer.get("TailscaleIPs") or [""])[0]
                peer_list.append({
                    "id": peer_id,
                    "name": name,
                    "ip": ip,
                    "active": bool(peer.get("ExitNode")),
                    "online": peer.get("Online", False),
                })
                if peer.get("ExitNode"):
                    current_exit = name
        return jsonify({"current": current_exit, "peers": peer_list})
    except Exception as exc:
        return jsonify({"error": str(exc), "current": None, "peers": []}), 500


@app.route("/api/tailscale/exit-node", methods=["POST"])
@require_auth_always
def api_tailscale_exit_node_post():
    """Set or clear the Tailscale exit node."""
    body = request.get_json(silent=True) or {}
    node = str(body.get("node", "")).strip()  # IP or hostname, or "" to clear
    if node:
        # Validate: no shell chars
        if any(c in node for c in (';', '&', '|', '`', '$', '>', '<', ' ')):
            return jsonify({"error": "invalid node"}), 400
        out, _ = _run(["tailscale", "set", f"--exit-node={node}", "--exit-node-allow-lan-access=true"], timeout=10)
    else:
        out, _ = _run(["tailscale", "set", "--exit-node="], timeout=10)
    return jsonify({"ok": True, "node": node or None})


# ── mDNS service browser ──────────────────────────────────────────────────────

@app.route("/api/mdns/services", methods=["GET"])
@require_auth
def api_mdns_services():
    """Return discovered mDNS/Bonjour services via avahi-browse."""
    try:
        result = subprocess.run(
            ["avahi-browse", "-all", "-t", "-r", "-p"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        out = result.stdout
    except FileNotFoundError:
        return jsonify({"services": [], "error": "avahi-browse not available"}), 200
    except subprocess.TimeoutExpired:
        out = ""

    services = []
    seen = set()
    for line in (out or "").splitlines():
        # avahi-browse -p output format: type;iface;proto;name;stype;domain;hostname;addr;port;txt
        parts = line.split(";")
        if len(parts) < 9 or parts[0] != "=":
            continue
        _, iface, proto, name, stype, domain, hostname, addr, port = parts[:9]
        txt = ";".join(parts[9:]) if len(parts) > 9 else ""
        key = (name, stype, addr)
        if key in seen:
            continue
        seen.add(key)
        services.append({
            "name": name,
            "type": stype,
            "hostname": hostname,
            "addr": addr,
            "port": int(port) if port.isdigit() else 0,
            "proto": proto,
            "iface": iface,
            "txt": txt[:256],
        })
    # Sort: by type then name
    services.sort(key=lambda s: (s["type"], s["name"].lower()))
    return jsonify({"services": services[:50]})


# ── Scheduled tasks viewer ────────────────────────────────────────────────────

import glob as _glob


@app.route("/api/cron/jobs", methods=["GET"])
@require_auth
def api_cron_jobs():
    """Return parsed cron jobs from /etc/cron.d/travel-router-* files."""
    jobs = []
    pattern = "/etc/cron.d/travel-router-*"
    try:
        files = sorted(_glob.glob(pattern))
    except Exception:
        files = []

    for fpath in files:
        try:
            content = Path(fpath).read_text()
        except OSError:
            continue
        fname = fpath.rsplit("/", 1)[-1]
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # cron format: min hour dom month dow user command
            parts = line.split(None, 6)
            if len(parts) < 7:
                continue
            minute, hour, dom, month, dow, user, command = parts
            # Build human-readable schedule string
            schedule = _cron_human(minute, hour, dom, month, dow)
            jobs.append({
                "file": fname,
                "minute": minute,
                "hour": hour,
                "dom": dom,
                "month": month,
                "dow": dow,
                "user": user,
                "command": command[:128],
                "schedule": schedule,
            })

    # Also check /etc/cron.d/travel-router (without suffix) if exists
    try:
        extra = Path("/etc/cron.d/travel-router").read_text()
        for line in extra.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 6)
            if len(parts) < 7:
                continue
            minute, hour, dom, month, dow, user, command = parts
            jobs.append({
                "file": "travel-router",
                "minute": minute, "hour": hour, "dom": dom,
                "month": month, "dow": dow, "user": user,
                "command": command[:128],
                "schedule": _cron_human(minute, hour, dom, month, dow),
            })
    except OSError:
        pass

    return jsonify({"jobs": jobs})


def _cron_human(minute: str, hour: str, dom: str, month: str, dow: str) -> str:
    """Convert cron fields to a human-readable string."""
    days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    if minute == "*/5" and hour == "*":
        return "Every 5 minutes"
    if minute == "*/10" and hour == "*":
        return "Every 10 minutes"
    if minute == "*/15" and hour == "*":
        return "Every 15 minutes"
    if minute == "*/30" and hour == "*":
        return "Every 30 minutes"
    if minute == "*" and hour == "*":
        return "Every minute"
    if hour == "*" and minute.isdigit():
        return f"At :{minute.zfill(2)} every hour"
    if hour.isdigit() and minute.isdigit():
        t = f"{int(hour):02d}:{int(minute):02d}"
        if dom == "*" and dow == "*":
            return f"Daily at {t}"
        if dow != "*" and dom == "*":
            try:
                day_name = days[int(dow) % 7]
                return f"Weekly on {day_name} at {t}"
            except (ValueError, IndexError):
                return f"Weekly ({dow}) at {t}"
        return f"At {t} ({dom}/{month} dow={dow})"
    return f"{minute} {hour} {dom} {month} {dow}"


# ── DNS hosts override ────────────────────────────────────────────────────────

DNS_HOSTS_FILE = "/etc/hosts.travel-router"
DNS_HOSTS_STORE = "/var/lib/travel-router/dns-hosts.json"


def _read_dns_hosts() -> list:
    """Returns list of {hostname, ip, comment} dicts from the JSON store."""
    try:
        text = Path(DNS_HOSTS_STORE).read_text().strip()
        if not text:
            return []
        return json.loads(text)
    except (OSError, json.JSONDecodeError):
        return []


def _write_dns_hosts(entries: list) -> None:
    """Write JSON store and regenerate /etc/hosts.travel-router atomically."""
    # Write JSON store
    d = str(Path(DNS_HOSTS_STORE).parent)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(entries, fh)
        os.replace(tmp, DNS_HOSTS_STORE)
    except Exception:
        import contextlib
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    # Regenerate /etc/hosts.travel-router
    lines = ["# Auto-generated by travel-router — do not edit manually\n"]
    for e in entries:
        comment = f"  # {e['comment']}" if e.get("comment") else ""
        lines.append(f"{e['ip']}\t{e['hostname']}{comment}\n")
    try:
        fd2, tmp2 = tempfile.mkstemp(dir="/etc")
        with os.fdopen(fd2, "w") as fh:
            fh.writelines(lines)
        os.replace(tmp2, DNS_HOSTS_FILE)
        # Signal dnsmasq to reload
        _run(["pkill", "-HUP", "dnsmasq"], timeout=3)
    except Exception:
        pass  # Best-effort; don't fail if /etc is read-only on dev machine


_HOSTNAME_RE = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9\-\.]{0,61}[a-zA-Z0-9])?$')


@app.route("/api/dns/hosts", methods=["GET"])
@require_auth
def api_dns_hosts_get():
    return jsonify({"entries": _read_dns_hosts()})


@app.route("/api/dns/hosts", methods=["POST"])
@require_auth_always
def api_dns_hosts_post():
    body = request.get_json(silent=True) or {}
    hostname = str(body.get("hostname", "")).strip().lower()
    ip = str(body.get("ip", "")).strip()
    comment = str(body.get("comment", ""))[:64]
    if not _HOSTNAME_RE.match(hostname):
        return jsonify({"error": "invalid hostname"}), 400
    if not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip) or \
            any(int(o) > 255 for o in ip.split(".")):
        return jsonify({"error": "invalid IP address"}), 400
    entries = _read_dns_hosts()
    # Update existing or append
    for e in entries:
        if e["hostname"] == hostname:
            e["ip"] = ip
            e["comment"] = comment
            _write_dns_hosts(entries)
            return jsonify({"ok": True, "updated": True})
    entries.append({"hostname": hostname, "ip": ip, "comment": comment})
    _write_dns_hosts(entries)
    return jsonify({"ok": True, "updated": False})


@app.route("/api/dns/hosts/<hostname>", methods=["DELETE"])
@require_auth_always
def api_dns_hosts_delete(hostname):
    entries = _read_dns_hosts()
    new_entries = [e for e in entries if e["hostname"] != hostname.lower()]
    if len(new_entries) == len(entries):
        return jsonify({"error": "not found"}), 404
    _write_dns_hosts(new_entries)
    return jsonify({"ok": True})


# ── Static DHCP reservations ──────────────────────────────────────────────────

DHCP_RESERVATIONS_FILE = "/var/lib/travel-router/dhcp-reservations.json"
DNSMASQ_RESERVATIONS_CONF = "/etc/dnsmasq.d/99-travel-router-reservations.conf"

_MAC_RE = re.compile(r'^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$')


def _read_dhcp_reservations() -> list:
    try:
        text = Path(DHCP_RESERVATIONS_FILE).read_text().strip()
        return json.loads(text) if text else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_dhcp_reservations(entries: list) -> None:
    """Write JSON store and regenerate dnsmasq reservation conf atomically."""
    # Write JSON store
    d = str(Path(DHCP_RESERVATIONS_FILE).parent)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(entries, fh)
        os.replace(tmp, DHCP_RESERVATIONS_FILE)
    except Exception:
        import contextlib
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    # Regenerate dnsmasq conf
    lines = ["# Auto-generated by travel-router — do not edit manually\n"]
    for e in entries:
        comment = f"  # {e['hostname']}" if e.get("hostname") else ""
        lines.append(f"dhcp-host={e['mac']},{e['ip']}{comment}\n")
    try:
        fd2, tmp2 = tempfile.mkstemp(dir="/etc/dnsmasq.d")
        with os.fdopen(fd2, "w") as fh:
            fh.writelines(lines)
        os.replace(tmp2, DNSMASQ_RESERVATIONS_CONF)
        _run(["systemctl", "reload", "dnsmasq"], timeout=5)
    except Exception:
        pass  # Best-effort


@app.route("/api/dhcp/reservations", methods=["GET"])
@require_auth
def api_dhcp_reservations_get():
    return jsonify({"reservations": _read_dhcp_reservations()})


@app.route("/api/dhcp/reservations", methods=["POST"])
@require_auth_always
def api_dhcp_reservations_post():
    body = request.get_json(silent=True) or {}
    mac = str(body.get("mac", "")).strip().lower()
    ip = str(body.get("ip", "")).strip()
    hostname = str(body.get("hostname", ""))[:64].strip()
    if not _MAC_RE.match(mac):
        return jsonify({"error": "invalid MAC address"}), 400
    if not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip) or \
            any(int(o) > 255 for o in ip.split(".")):
        return jsonify({"error": "invalid IP address"}), 400
    entries = _read_dhcp_reservations()
    for e in entries:
        if e["mac"] == mac:
            e["ip"] = ip
            e["hostname"] = hostname
            _write_dhcp_reservations(entries)
            return jsonify({"ok": True, "updated": True})
    entries.append({"mac": mac, "ip": ip, "hostname": hostname})
    _write_dhcp_reservations(entries)
    return jsonify({"ok": True, "updated": False})


@app.route("/api/dhcp/reservations/<mac>", methods=["DELETE"])
@require_auth_always
def api_dhcp_reservations_delete(mac):
    mac = mac.lower()
    entries = _read_dhcp_reservations()
    new_entries = [e for e in entries if e["mac"] != mac]
    if len(new_entries) == len(entries):
        return jsonify({"error": "not found"}), 404
    _write_dhcp_reservations(new_entries)
    return jsonify({"ok": True})


# ── Active DHCP leases ────────────────────────────────────────────────────────

DNSMASQ_LEASES_FILE = "/var/lib/misc/dnsmasq.leases"


@app.route("/api/dhcp/leases", methods=["GET"])
@require_auth
def api_dhcp_leases():
    """Return active DHCP leases from dnsmasq.leases file."""
    import time as _time
    now = int(_time.time())
    leases = []
    try:
        text = Path(DNSMASQ_LEASES_FILE).read_text()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            # Format: <expiry_epoch> <mac> <ip> <hostname> <client-id>
            if len(parts) < 4:
                continue
            try:
                expiry = int(parts[0])
                mac = parts[1]
                ip = parts[2]
                hostname = parts[3] if parts[3] != "*" else ""
                client_id = parts[4] if len(parts) > 4 and parts[4] != "*" else ""
                remaining_s = expiry - now
                expired = remaining_s <= 0
                # Format remaining time
                if expired:
                    remaining_str = "expired"
                elif remaining_s < 60:
                    remaining_str = f"{remaining_s}s"
                elif remaining_s < 3600:
                    remaining_str = f"{remaining_s // 60}m {remaining_s % 60}s"
                elif remaining_s < 86400:
                    h = remaining_s // 3600
                    m = (remaining_s % 3600) // 60
                    remaining_str = f"{h}h {m}m"
                else:
                    d = remaining_s // 86400
                    h = (remaining_s % 86400) // 3600
                    remaining_str = f"{d}d {h}h"
                leases.append({
                    "mac": mac,
                    "ip": ip,
                    "hostname": hostname,
                    "expiry": expiry,
                    "remaining_s": remaining_s,
                    "remaining": remaining_str,
                    "expired": expired,
                    "client_id": client_id,
                })
            except (ValueError, IndexError):
                continue
    except OSError:
        pass
    # Sort by IP address
    leases.sort(key=lambda x: tuple(int(o) for o in x["ip"].split(".") if o.isdigit()))
    return jsonify({"leases": leases, "count": len(leases), "file": DNSMASQ_LEASES_FILE})


# ── LAN network scanner ───────────────────────────────────────────────────────

@app.route("/api/network/scan", methods=["GET"])
@require_auth
def api_network_scan():
    """Scan the AP subnet for live hosts using ARP + ping sweep."""
    # Determine AP subnet from ip route
    subnet = "10.3.141.0/24"
    try:
        out, _ = _run(["ip", "route", "show", "dev", "uap0"], timeout=3)
        for line in (out or "").splitlines():
            parts = line.split()
            if parts and "/" in parts[0]:
                subnet = parts[0]
                break
    except Exception:
        pass

    hosts = []

    # Method 1: arp-scan (fast, comprehensive)
    try:
        out, _ = _run(["arp-scan", "--localnet", "--interface=uap0", "--quiet"], timeout=15)
        for line in (out or "").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].count(".") == 3:
                ip = parts[0].strip()
                mac = parts[1].strip() if len(parts) > 1 else ""
                vendor = parts[2].strip() if len(parts) > 2 else ""
                hosts.append({"ip": ip, "mac": mac, "vendor": vendor, "method": "arp-scan"})
    except Exception:
        pass

    # Method 2: ARP table fallback (instant, no root needed)
    if not hosts:
        try:
            out, _ = _run(["ip", "neigh", "show", "dev", "uap0"], timeout=3)
            for line in (out or "").splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[2] == "lladdr":
                    ip = parts[0]
                    mac = parts[4] if len(parts) > 4 else parts[3]
                    state = parts[-1] if parts[-1] in ("REACHABLE", "STALE", "DELAY", "PROBE", "FAILED") else ""
                    hosts.append({"ip": ip, "mac": mac, "vendor": "", "method": "arp", "state": state})
        except Exception:
            pass

    # Deduplicate by IP
    seen = {}
    for h in hosts:
        seen[h["ip"]] = h
    hosts = sorted(seen.values(), key=lambda x: tuple(int(o) for o in x["ip"].split(".") if o.isdigit()))

    return jsonify({"subnet": subnet, "hosts": hosts, "count": len(hosts)})


# ── Journal log viewer ────────────────────────────────────────────────────────

_JOURNAL_ALLOWED_UNITS = {
    "hostapd", "dnsmasq", "wg-quick@wg0", "tailscaled",
    "travel-router-web", "systemd-networkd", "dhcpcd",
    "failover-watchdog", "wan-watchdog", "tailscale-watchdog",
    "wg-key-rotate", "wg-peer-expire", "aide-check",
}

@app.route("/api/system/journal", methods=["GET"])
@require_auth
def api_system_journal():
    """Return journalctl output for a specific service unit."""
    unit = request.args.get("unit", "travel-router-web").strip()
    if unit not in _JOURNAL_ALLOWED_UNITS:
        return jsonify({"error": f"unit not allowed: {unit}", "allowed": sorted(_JOURNAL_ALLOWED_UNITS)}), 400
    lines = min(int(request.args.get("lines", 100)), 500)
    out, rc = _run(
        ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=short-iso"],
        timeout=10,
    )
    log_text = out.strip() if out and out.strip() else "(no journal entries found)"
    return jsonify({
        "unit": unit,
        "lines": lines,
        "output": log_text,
        "allowed_units": sorted(_JOURNAL_ALLOWED_UNITS),
    })


# ── Travel-router config editor ───────────────────────────────────────────────

_TRAVEL_ROUTER_CONFIG = "/etc/default/travel-router"

# Keys that are safe to display and edit (allowlist)
_CONFIG_EDITABLE_KEYS = {
    "NTFY_TOPIC", "NTFY_SERVER", "IPHONE_BT_MAC", "AP_SSID", "AP_SUBNET",
    "AP_GATEWAY", "ENABLE_DOT", "ENABLE_ADGUARD", "ENABLE_VPN_KILLSWITCH",
    "ENABLE_TOR_TRANSPARENT", "ENABLE_CLIENT_QOS", "ENABLE_CAKE_AUTOTUNE",
    "ENABLE_BANDWIDTH_DASHBOARD", "ENABLE_AP_SCHEDULE", "AP_DISABLE_TIME",
    "AP_ENABLE_TIME", "ENABLE_AUTO_UPDATES", "UPS_SHUTDOWN_THRESHOLD",
    "FAILOVER_PROBE_TIMEOUT", "SPLIT_TUNNEL_DOMAINS", "ENABLE_SPLIT_TUNNEL",
}


def _read_travel_router_config() -> dict:
    """Parse /etc/default/travel-router into a dict of key->value."""
    cfg = {}
    try:
        text = Path(_TRAVEL_ROUTER_CONFIG).read_text()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key in _CONFIG_EDITABLE_KEYS:
                    cfg[key] = val
    except OSError:
        pass
    return cfg


@app.route("/api/config/travel-router", methods=["GET"])
@require_auth
def api_config_get():
    """Return editable config keys from /etc/default/travel-router."""
    cfg = _read_travel_router_config()
    return jsonify({"config": cfg, "editable_keys": sorted(_CONFIG_EDITABLE_KEYS)})


@app.route("/api/config/travel-router", methods=["POST"])
@require_auth_always
def api_config_post():
    """Update one or more config keys in /etc/default/travel-router."""
    body = request.get_json(silent=True) or {}
    updates = body.get("updates", {})
    if not isinstance(updates, dict):
        return jsonify({"error": "updates must be a dict"}), 400
    # Validate keys
    bad_keys = [k for k in updates if k not in _CONFIG_EDITABLE_KEYS]
    if bad_keys:
        return jsonify({"error": f"keys not editable: {bad_keys}"}), 400
    # Validate values: no newlines, no shell injection
    for k, v in updates.items():
        if not isinstance(v, str):
            return jsonify({"error": f"value for {k} must be a string"}), 400
        if "\n" in v or "\r" in v:
            return jsonify({"error": f"value for {k} contains newlines"}), 400
    try:
        try:
            text = Path(_TRAVEL_ROUTER_CONFIG).read_text()
        except OSError:
            text = ""
        lines = text.splitlines()
        applied = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key in updates:
                    new_lines.append(f'{key}="{updates[key]}"')
                    applied.add(key)
                    continue
            new_lines.append(line)
        # Append any keys not already present in the file
        for key, val in updates.items():
            if key not in applied:
                new_lines.append(f'{key}="{val}"')
        new_text = "\n".join(new_lines) + "\n"
        # Atomic write
        d = str(Path(_TRAVEL_ROUTER_CONFIG).parent)
        fd, tmp = tempfile.mkstemp(dir=d)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(new_text)
            os.replace(tmp, _TRAVEL_ROUTER_CONFIG)
        except Exception:
            import contextlib
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return jsonify({"ok": True, "updated": list(updates.keys())})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Ping monitor ──────────────────────────────────────────────────────────────

PING_HOSTS_FILE = "/var/lib/travel-router/ping-hosts.json"


def _read_ping_hosts() -> list:
    try:
        text = Path(PING_HOSTS_FILE).read_text().strip()
        return json.loads(text) if text else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_ping_hosts(hosts: list) -> None:
    d = str(Path(PING_HOSTS_FILE).parent)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(hosts, fh)
        os.replace(tmp, PING_HOSTS_FILE)
    except Exception:
        import contextlib
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _ping_host(host: str) -> dict:
    """Ping a host once, return {host, up, latency_ms}."""
    try:
        out, _ = _run(["ping", "-c", "1", "-W", "2", host], timeout=5)
        if out and "1 received" in out:
            # Parse time= from ping output
            import re as _re
            m = _re.search(r'time=(\d+\.?\d*)\s*ms', out)
            latency = float(m.group(1)) if m else None
            return {"host": host, "up": True, "latency_ms": latency}
        return {"host": host, "up": False, "latency_ms": None}
    except Exception:
        return {"host": host, "up": False, "latency_ms": None}


@app.route("/api/monitor/ping", methods=["GET"])
@require_auth
def api_monitor_ping_get():
    """Return stored host list with current up/down status."""
    hosts = _read_ping_hosts()
    results = [_ping_host(h["host"]) for h in hosts]
    # Merge label back in
    labels = {h["host"]: h.get("label", "") for h in hosts}
    for r in results:
        r["label"] = labels.get(r["host"], "")
    return jsonify({"results": results})


@app.route("/api/monitor/ping", methods=["POST"])
@require_auth_always
def api_monitor_ping_post():
    """Add a host to the ping monitor list."""
    body = request.get_json(silent=True) or {}
    host = str(body.get("host", "")).strip()
    label = str(body.get("label", ""))[:64].strip()
    if not host or len(host) > 253:
        return jsonify({"error": "invalid host"}), 400
    # Basic validation: no spaces, no shell chars
    if any(c in host for c in (' ', ';', '&', '|', '`', '$', '>', '<')):
        return jsonify({"error": "invalid host"}), 400
    hosts = _read_ping_hosts()
    if any(h["host"] == host for h in hosts):
        return jsonify({"error": "host already monitored"}), 409
    if len(hosts) >= 20:
        return jsonify({"error": "maximum 20 hosts"}), 400
    hosts.append({"host": host, "label": label})
    _write_ping_hosts(hosts)
    return jsonify({"ok": True})


@app.route("/api/monitor/ping/<path:host>", methods=["DELETE"])
@require_auth_always
def api_monitor_ping_delete(host):
    hosts = _read_ping_hosts()
    new_hosts = [h for h in hosts if h["host"] != host]
    if len(new_hosts) == len(hosts):
        return jsonify({"error": "not found"}), 404
    _write_ping_hosts(new_hosts)
    return jsonify({"ok": True})


# ── Network interface stats ───────────────────────────────────────────────────

@app.route("/api/network/interfaces", methods=["GET"])
@require_auth
def api_network_interfaces():
    """Return per-interface stats from /proc/net/dev."""
    interfaces = []
    try:
        text = Path("/proc/net/dev").read_text()
        lines = text.splitlines()
        # Skip 2-line header
        for line in lines[2:]:
            line = line.strip()
            if not line:
                continue
            iface, _, data = line.partition(":")
            iface = iface.strip()
            # Skip loopback
            if iface == "lo":
                continue
            parts = data.split()
            if len(parts) < 16:
                continue
            try:
                rx_bytes = int(parts[0])
                rx_packets = int(parts[1])
                rx_errs = int(parts[2])
                rx_drop = int(parts[3])
                tx_bytes = int(parts[8])
                tx_packets = int(parts[9])
                tx_errs = int(parts[10])
                tx_drop = int(parts[11])
            except (ValueError, IndexError):
                continue
            # Get link state
            operstate = "unknown"
            try:
                operstate = Path(f"/sys/class/net/{iface}/operstate").read_text().strip()
            except OSError:
                pass
            interfaces.append({
                "interface": iface,
                "operstate": operstate,
                "rx_bytes": rx_bytes,
                "rx_packets": rx_packets,
                "rx_errors": rx_errs,
                "rx_dropped": rx_drop,
                "tx_bytes": tx_bytes,
                "tx_packets": tx_packets,
                "tx_errors": tx_errs,
                "tx_dropped": tx_drop,
            })
    except OSError:
        pass
    return jsonify({"interfaces": interfaces})


# ── ntfy notification management ──────────────────────────────────────────────

@app.route("/api/notify/config", methods=["GET"])
@require_auth
def api_notify_config_get():
    """Return current ntfy configuration from /etc/default/travel-router."""
    topic = ""
    server = "https://ntfy.sh"
    try:
        text = Path("/etc/default/travel-router").read_text()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("NTFY_TOPIC="):
                topic = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("NTFY_SERVER="):
                server = line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return jsonify({"topic": topic, "server": server or "https://ntfy.sh",
                    "configured": bool(topic)})


@app.route("/api/notify/test", methods=["POST"])
@require_auth_always
def api_notify_test():
    """Send a test notification via ntfy."""
    body = request.get_json(silent=True) or {}
    topic = str(body.get("topic", "")).strip()
    server = str(body.get("server", "https://ntfy.sh")).strip().rstrip("/")
    if not topic:
        # Read from config
        try:
            text = Path("/etc/default/travel-router").read_text()
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("NTFY_TOPIC="):
                    topic = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("NTFY_SERVER="):
                    s = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if s:
                        server = s
        except OSError:
            pass
    if not topic:
        return jsonify({"error": "NTFY_TOPIC not configured"}), 400
    # Validate: no shell chars, reasonable length
    if any(c in topic for c in (';', '&', '|', '`', '$', '>', '<', ' ', '"', "'")):
        return jsonify({"error": "invalid topic"}), 400
    if len(topic) > 100:
        return jsonify({"error": "topic too long"}), 400
    url = f"{server}/{topic}"
    import time as _time
    message = f"Travel Router test notification — {_time.strftime('%H:%M:%S')}"
    try:
        out, _ = _run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-d", message,
             "-H", "Title: Travel Router Test",
             "-H", "Priority: default",
             url],
            timeout=10,
        )
        status_code = int((out or "0").strip())
        if 200 <= status_code < 300:
            return jsonify({"ok": True, "url": url, "status": status_code})
        return jsonify({"error": f"ntfy returned HTTP {status_code}", "url": url}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── DoH resolver selector ─────────────────────────────────────────────────────

_DOH_RESOLVERS = {
    "cloudflare": {"name": "Cloudflare", "url": "https://1.1.1.1/dns-query"},
    "quad9":      {"name": "Quad9",      "url": "https://dns.quad9.net/dns-query"},
    "google":     {"name": "Google",     "url": "https://dns.google/dns-query"},
    "nextdns":    {"name": "NextDNS",    "url": "https://dns.nextdns.io/"},
    "adguard":    {"name": "AdGuard",    "url": "https://dns.adguard-dns.com/dns-query"},
}

# Resolvers that set-doh-resolver.sh accepts by name (no https:// needed)
_DOH_SCRIPT_PRESETS = {"cloudflare", "quad9", "nextdns", "adguard", "system"}


@app.route("/api/dns/doh-resolver", methods=["GET"])
@require_auth
def api_doh_resolver_get():
    """Return current DoH resolver and available options."""
    current = "cloudflare"
    # Try to read current resolver from /etc/default/travel-router
    try:
        text = Path(DEFAULTS_FILE).read_text()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("DOH_RESOLVER=") or line.startswith("DNS_RESOLVER="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
                if val in _DOH_RESOLVERS:
                    current = val
                    break
                # Match by URL
                for name, info in _DOH_RESOLVERS.items():
                    if val == info["url"]:
                        current = name
                        break
                break
    except OSError:
        pass
    # Also check systemd-resolved DoH config
    try:
        resolved_conf = Path("/etc/systemd/resolved.conf.d/doh.conf")
        text = resolved_conf.read_text()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("DNS="):
                dns_host = line.split("=", 1)[1].strip().lower()
                for name, info in _DOH_RESOLVERS.items():
                    if dns_host in info["url"]:
                        current = name
                        break
                break
    except OSError:
        pass
    return jsonify({
        "current": current,
        "resolvers": [{"id": k, **v} for k, v in _DOH_RESOLVERS.items()],
    })


@app.route("/api/dns/doh-resolver", methods=["POST"])
@require_auth_always
def api_doh_resolver_post():
    """Switch DoH resolver via set-doh-resolver.sh."""
    body = request.get_json(silent=True) or {}
    resolver = str(body.get("resolver", "")).strip().lower()
    if resolver not in _DOH_RESOLVERS:
        return jsonify({"error": f"unknown resolver: {resolver}", "valid": list(_DOH_RESOLVERS.keys())}), 400
    # Determine what to pass to the script:
    # - known script presets → pass by name
    # - others (e.g. google) → pass by URL
    if resolver in _DOH_SCRIPT_PRESETS:
        script_arg = resolver
    else:
        script_arg = _DOH_RESOLVERS[resolver]["url"]
    script = Path(DOH_SCRIPT)
    if script.exists():
        out, rc = _run(["bash", str(script), script_arg], timeout=15)
        if rc != 0:
            return jsonify({"error": out.strip() or "Script failed", "resolver": resolver}), 503
        _push_event("doh_change", {"resolver": resolver})
        return jsonify({"ok": True, "resolver": resolver, "output": (out or "").strip()})
    # Fallback: record in config only
    _push_event("doh_change", {"resolver": resolver})
    return jsonify({
        "ok": True,
        "resolver": resolver,
        "note": "set-doh-resolver.sh not installed; set DOH_RESOLVER in /etc/default/travel-router",
    })


# ── Login History ─────────────────────────────────────────────────────────────

@app.route("/api/system/logins", methods=["GET"])
@require_auth
def api_system_logins():
    """Return recent login history from `last` command."""
    import re

    entries = []

    out, rc = _run(["last", "-n", "30", "-F"])
    if rc != 0:
        # Try without -F (some systems don't support it)
        out, rc = _run(["last", "-n", "30"])
    if rc != 0:
        return jsonify({"error": "last command not available", "entries": []})

    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("wtmp") or line.startswith("btmp") or line.startswith("reboot"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        user = parts[0]
        tty = parts[1]
        host = parts[2] if len(parts) > 2 else ""
        # Don't include system pseudo-logins
        if user in ("reboot", "shutdown", "runlevel", "LOGIN"):
            continue
        # Rest of line is date info
        date_str = " ".join(parts[3:])
        # Check for 'still logged in' or 'logged in' vs duration
        still_logged_in = "still logged in" in line or "logged in" in line
        entries.append({
            "user": user,
            "tty": tty,
            "host": host if host and not host.startswith("Mon") and not host.startswith("Tue") and not host.startswith("Wed") and not host.startswith("Thu") and not host.startswith("Fri") and not host.startswith("Sat") and not host.startswith("Sun") else "",
            "date_raw": date_str[:40],
            "active": still_logged_in,
        })
        if len(entries) >= 20:
            break

    # Also check currently logged in users via `who`
    who_out, who_rc = _run(["who"])
    active_users = set()
    if who_rc == 0:
        for wline in who_out.splitlines():
            wparts = wline.split()
            if wparts:
                active_users.add(wparts[0])

    # Mark active
    for e in entries:
        if e["user"] in active_users:
            e["active"] = True

    return jsonify({"entries": entries, "count": len(entries), "active_users": list(active_users)})


# ── DNS lookup tool ───────────────────────────────────────────────────────────

@app.route("/api/dns/lookup", methods=["GET"])
@require_auth
def api_dns_lookup():
    """Run a DNS lookup for a hostname."""
    host = request.args.get("host", "").strip()
    record_type = request.args.get("type", "A").strip().upper()
    if not host:
        return jsonify({"error": "host parameter required"}), 400
    valid_types = {"A", "AAAA", "MX", "TXT", "CNAME", "NS", "PTR", "SOA"}
    if record_type not in valid_types:
        record_type = "A"
    try:
        out, rc = _run(["dig", "+short", f"-t{record_type}", host], timeout=10)
        if rc != 0 or not out.strip():
            # Fall back to nslookup
            out2, rc2 = _run(["nslookup", "-type=" + record_type, host], timeout=10)
            records = [l.strip() for l in (out2 or "").splitlines() if l.strip() and not l.startswith(("Server:", "Address:", "Non-authoritative"))]
        else:
            records = [l.strip() for l in (out or "").splitlines() if l.strip()]
        return jsonify({"host": host, "type": record_type, "records": records, "rc": rc})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── ARP / neighbour table ─────────────────────────────────────────────────────

@app.route("/api/network/arp", methods=["GET"])
@require_auth
def api_network_arp():
    """Return the current ARP/neighbour table."""
    neighbors = []
    try:
        out, _ = _run(["ip", "neigh", "show"], timeout=5)
        for line in (out or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            # Format: <ip> dev <iface> lladdr <mac> <state>
            ip_addr = parts[0] if parts else ""
            entry = {"ip": ip_addr, "dev": "", "mac": "", "state": ""}
            for i, p in enumerate(parts):
                if p == "dev" and i + 1 < len(parts):
                    entry["dev"] = parts[i + 1]
                elif p == "lladdr" and i + 1 < len(parts):
                    entry["mac"] = parts[i + 1]
            # Last token is state (REACHABLE, STALE, FAILED, etc.)
            if parts:
                entry["state"] = parts[-1]
            neighbors.append(entry)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"neighbors": neighbors, "count": len(neighbors)})


# ── System Update Checker ─────────────────────────────────────────────────────

@app.route("/api/system/updates", methods=["GET"])
@require_auth
def api_system_updates():
    """Return list of available package updates from apt."""
    import re

    packages = []
    error = None

    # Run apt list --upgradable (fast, no network — uses cached apt metadata)
    out, rc = _run(["apt", "list", "--upgradable"])
    if rc != 0:
        error = "apt not available"
    else:
        for line in out.splitlines():
            # Format: package/suite version arch [upgradable from: old_version]
            if line.startswith("Listing") or not line.strip():
                continue
            m = re.match(r'^(\S+)/(\S+)\s+(\S+)\s+(\S+)(?:\s+\[upgradable from: (\S+)\])?', line)
            if m:
                pkg_full = m.group(1)
                pkg_name = pkg_full.split(":")[0]  # strip arch suffix
                suite = m.group(2)
                new_ver = m.group(3)
                arch = m.group(4)
                old_ver = m.group(5) or ""
                # Flag security updates
                is_security = "security" in suite
                packages.append({
                    "name": pkg_name,
                    "new_version": new_ver,
                    "old_version": old_ver,
                    "suite": suite,
                    "arch": arch,
                    "security": is_security,
                })

    # Check when apt cache was last updated
    last_update = None
    try:
        import os
        stamp = "/var/cache/apt/pkgcache.bin"
        if os.path.exists(stamp):
            mtime = os.path.getmtime(stamp)
            import time
            age_sec = int(time.time() - mtime)
            if age_sec < 3600:
                last_update = f"{age_sec // 60}m ago"
            elif age_sec < 86400:
                last_update = f"{age_sec // 3600}h ago"
            else:
                last_update = f"{age_sec // 86400}d ago"
    except OSError:
        pass

    security_count = sum(1 for p in packages if p["security"])

    return jsonify({
        "packages": packages,
        "count": len(packages),
        "security_count": security_count,
        "last_cache_update": last_update,
        "error": error,
    })


# ── Config backup ─────────────────────────────────────────────────────────────

@app.route("/api/config/backup", methods=["GET"])
@require_auth_always
def api_config_backup():
    """Generate and download a tar.gz of key config files."""
    import io
    import tarfile
    import datetime

    backup_paths = [
        "/etc/default/travel-router",
        "/etc/wireguard/wg0.conf",
        "/etc/dnsmasq.conf",
        "/etc/dnsmasq.d/",
        "/var/lib/travel-router/",
        "/etc/hostapd/hostapd.conf",
    ]
    buf = io.BytesIO()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path in backup_paths:
                p = Path(path)
                if p.exists():
                    tar.add(str(p), arcname=path.lstrip("/"))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    buf.seek(0)
    from flask import send_file
    return send_file(
        buf,
        mimetype="application/gzip",
        as_attachment=True,
        download_name=f"travel-router-backup-{ts}.tar.gz",
    )


# ── Traceroute tool ───────────────────────────────────────────────────────────

@app.route("/api/network/traceroute", methods=["GET"])
@require_auth
def api_network_traceroute():
    """Run traceroute/tracepath to a target host."""
    target = request.args.get("host", "").strip()
    if not target:
        return jsonify({"error": "host parameter required"}), 400
    # Basic validation — no shell injection
    import re
    if not re.match(r'^[a-zA-Z0-9.\-:_]+$', target):
        return jsonify({"error": "invalid host"}), 400
    hops = []
    try:
        # Try traceroute first, fall back to tracepath
        out, rc = _run(["traceroute", "-n", "-m", "20", "-w", "2", target], timeout=45)
        if rc != 0 or not out.strip():
            out, rc = _run(["tracepath", "-n", target], timeout=45)
        for line in (out or "").splitlines():
            line = line.strip()
            if not line:
                continue
            hops.append(line)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"target": target, "hops": hops, "count": len(hops)})


# ── Wi-Fi Clients ─────────────────────────────────────────────────────────────


@app.route("/api/wifi/clients")
@require_auth
def api_wifi_clients():
    """Return connected Wi-Fi station info from iw station dump."""

    def _parse_stations(output):
        clients = []
        current = {}
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if line.startswith("Station "):
                if current.get("mac"):
                    clients.append(current)
                mac = line.split()[1]
                current = {"mac": mac, "signal_dbm": None, "signal_quality": 0,
                           "rx_bytes": 0, "tx_bytes": 0, "inactive_ms": 0}
            elif line.startswith("signal:") and current:
                # e.g. "signal:  -55 dBm"
                m = re.search(r"(-?\d+)\s*dBm", line)
                if m:
                    dbm = int(m.group(1))
                    current["signal_dbm"] = dbm
                    current["signal_quality"] = max(0, min(100, 2 * (dbm + 100)))
            elif line.startswith("rx bytes:") and current:
                m = re.search(r"(\d+)", line)
                if m:
                    current["rx_bytes"] = int(m.group(1))
            elif line.startswith("tx bytes:") and current:
                m = re.search(r"(\d+)", line)
                if m:
                    current["tx_bytes"] = int(m.group(1))
            elif line.startswith("inactive time:") and current:
                m = re.search(r"(\d+)\s*ms", line)
                if m:
                    current["inactive_ms"] = int(m.group(1))
        if current.get("mac"):
            clients.append(current)
        return clients

    iface = "wlan0"
    out, rc = _run(["iw", "dev", "wlan0", "station", "dump"])
    clients = _parse_stations(out) if rc == 0 else []

    if not clients:
        out1, rc1 = _run(["iw", "dev", "wlan1", "station", "dump"])
        if rc1 == 0 and out1.strip():
            clients = _parse_stations(out1)
            iface = "wlan1"

    return jsonify({"interface": iface, "clients": clients, "count": len(clients)})


# ── Listening Ports ───────────────────────────────────────────────────────────

@app.route("/api/network/ports", methods=["GET"])
@require_auth
def api_network_ports():
    """Return open listening ports on the router via ss (or netstat fallback)."""
    import re

    ports = []

    def parse_ss(output):
        results = []
        for line in output.splitlines():
            line = line.strip()
            # Match lines like: tcp  LISTEN  0  128  0.0.0.0:22  0.0.0.0:*  users:(("sshd",pid=1234,fd=3))
            parts = line.split()
            if len(parts) < 5:
                continue
            proto = parts[0].lower()
            state = parts[1]
            if state != "LISTEN":
                continue
            local = parts[4]
            # Split local address/port on last ':'
            colon = local.rfind(":")
            if colon == -1:
                continue
            local_addr = local[:colon]
            try:
                local_port = int(local[colon + 1:])
            except ValueError:
                continue
            # Extract PID and program from users:(("prog",pid=N,...))
            pid = None
            program = None
            users_match = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
            if users_match:
                program = users_match.group(1)
                pid = int(users_match.group(2))
            results.append({
                "proto": proto,
                "local_addr": local_addr,
                "local_port": local_port,
                "state": "LISTEN",
                "pid": pid,
                "program": program,
            })
        return results

    def parse_netstat(output):
        results = []
        for line in output.splitlines():
            line = line.strip()
            parts = line.split()
            if len(parts) < 6:
                continue
            proto = parts[0].lower()
            if proto not in ("tcp", "tcp6"):
                continue
            state = parts[5] if len(parts) > 5 else ""
            if state != "LISTEN":
                continue
            local = parts[3]
            colon = local.rfind(":")
            if colon == -1:
                continue
            local_addr = local[:colon]
            try:
                local_port = int(local[colon + 1:])
            except ValueError:
                continue
            pid = None
            program = None
            if len(parts) > 6:
                pid_prog = parts[6]
                m = re.match(r"(\d+)/(.+)", pid_prog)
                if m:
                    pid = int(m.group(1))
                    program = m.group(2)
            results.append({
                "proto": "tcp",
                "local_addr": local_addr,
                "local_port": local_port,
                "state": "LISTEN",
                "pid": pid,
                "program": program,
            })
        return results

    out, rc = _run(["ss", "-tlnup"])
    if rc != 0:
        out, rc = _run(["netstat", "-tlnup"])
        if rc == 0:
            ports = parse_netstat(out)
    else:
        ports = parse_ss(out)

    ports.sort(key=lambda x: x["local_port"])

    return jsonify({"ports": ports, "count": len(ports)})


# ── Storage / Block Device Info ───────────────────────────────────────────────


def _parse_size_to_gb(size_str: str) -> float:
    """Parse a human-readable size string (e.g. '14G', '256M', '1.5T') to GB float."""
    size_str = size_str.strip()
    if not size_str or size_str == "-":
        return 0.0
    try:
        unit = size_str[-1].upper()
        value = float(size_str[:-1])
        if unit == "T":
            return round(value * 1024.0, 3)
        if unit == "G":
            return round(value, 3)
        if unit == "M":
            return round(value / 1024.0, 3)
        if unit == "K":
            return round(value / 1048576.0, 6)
        # No unit — assume bytes
        return round(float(size_str) / (1024 ** 3), 6)
    except (ValueError, IndexError):
        return 0.0


@app.route("/api/system/storage", methods=["GET"])
@require_auth
def api_system_storage():
    """Return disk partition usage and USB device list."""
    disks = []
    out, rc = _run("df -h --output=source,target,fstype,size,used,avail,pcent")
    if rc == 0:
        skip_fs = {"tmpfs", "devtmpfs", "udev", "none", "overlay", "squashfs"}
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 7:
                continue
            device, mountpoint, fstype, size, used, avail, pcent = (
                parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6],
            )
            if fstype in skip_fs:
                continue
            try:
                percent = float(pcent.rstrip("%"))
            except ValueError:
                percent = 0.0
            disks.append({
                "device": device,
                "mountpoint": mountpoint,
                "fstype": fstype,
                "total_gb": _parse_size_to_gb(size),
                "used_gb": _parse_size_to_gb(used),
                "free_gb": _parse_size_to_gb(avail),
                "percent": percent,
            })

    usb_devices = []
    usb_out, usb_rc = _run("lsusb")
    if usb_rc == 0:
        import re as _re
        for line in usb_out.splitlines():
            # Format: Bus 001 Device 003: ID 0781:5583 SanDisk Ultra Fit
            m = _re.match(
                r"Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)",
                line,
            )
            if m:
                usb_devices.append({
                    "bus": m.group(1),
                    "device": m.group(2),
                    "vendor_id": m.group(3),
                    "product_id": m.group(4),
                    "description": m.group(5).strip(),
                })

    return jsonify({"disks": disks, "usb_devices": usb_devices})


# ── Ping Connectivity Checker ─────────────────────────────────────────────────


@app.route("/api/network/ping", methods=["GET"])
@require_auth
def api_network_ping():
    """Run ping to a target host and return packet loss + RTT stats."""
    import re as _re
    host = request.args.get("host", "").strip()
    if not host:
        return jsonify({"error": "host parameter required"}), 400
    if not _re.match(r'^[a-zA-Z0-9.\-:_]+$', host):
        return jsonify({"error": "invalid host"}), 400
    try:
        count = max(1, min(10, int(request.args.get("count", 4))))
    except (ValueError, TypeError):
        count = 4
    try:
        out, rc = _run(["ping", "-c", str(count), "-W", "2", host], timeout=30)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    transmitted = received = 0
    loss_percent = 100.0
    rtt_min = rtt_avg = rtt_max = None

    for line in (out or "").splitlines():
        # e.g. "4 packets transmitted, 4 received, 0% packet loss"
        m = _re.search(r'(\d+) packets transmitted,\s*(\d+) received,\s*([\d.]+)%', line)
        if m:
            transmitted = int(m.group(1))
            received = int(m.group(2))
            loss_percent = float(m.group(3))
        # e.g. "rtt min/avg/max/mdev = 12.3/14.1/16.2/1.5 ms"
        m = _re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/', line)
        if m:
            rtt_min = float(m.group(1))
            rtt_avg = float(m.group(2))
            rtt_max = float(m.group(3))

    alive = received > 0
    result = {
        "host": host,
        "count": count,
        "transmitted": transmitted,
        "received": received,
        "loss_percent": loss_percent,
        "rtt_min_ms": rtt_min,
        "rtt_avg_ms": rtt_avg,
        "rtt_max_ms": rtt_max,
        "alive": alive,
    }
    return jsonify(result)


# ── WireGuard Peer Health ─────────────────────────────────────────────────────

@app.route("/api/vpn/wireguard/peers", methods=["GET"])
@require_auth
def api_vpn_wireguard_peers():
    """Return WireGuard peer list with health stats from wg show."""
    out, rc = _run(["wg", "show", "all", "dump"])
    if rc != 0:
        return jsonify({"peers": [], "count": 0, "wg_available": False,
                        "error": "wg not available or no interfaces"})
    peers = []
    now = int(time.time())
    for line in out.strip().splitlines():
        parts = line.split("\t")
        # wg show all dump: iface pubkey preshared endpoint allowed_ips latest_handshake rx_bytes tx_bytes persistent_keepalive
        # Interface lines have 5 fields, peer lines have 9
        if len(parts) < 9:
            continue
        iface = parts[0]
        pubkey = parts[1]
        # Skip interface summary lines (preshared == "(none)" distinguishes peer lines, but
        # interface lines have exactly 5 fields — already filtered above by len < 9)
        if parts[2] == "(none)" or len(parts) == 5:
            continue
        try:
            endpoint = parts[3] if parts[3] != "(none)" else None
            allowed_ips_list = parts[4].split(",") if parts[4] else []
            allowed_ips_str = parts[4] if parts[4] != "(none)" else None
            latest_handshake = int(parts[5])
            rx_bytes = int(parts[6])
            tx_bytes = int(parts[7])
        except (ValueError, IndexError):
            continue

        if latest_handshake == 0:
            handshake_age = None
            handshake_label = "Never"
            age_sec = None
            age_human = "never"
            status = "inactive"
            health_status = "never"
        else:
            age = now - latest_handshake
            handshake_age = age
            age_sec = age
            age_human = _format_duration(age)
            if age < 180:
                status = "active"
                health_status = "recent"
            elif age < 600:
                status = "idle"
                health_status = "stale"
            else:
                status = "stale"
                health_status = "idle"
            # Human-readable age for legacy consumers
            if age < 60:
                handshake_label = f"{age}s ago"
            elif age < 3600:
                handshake_label = f"{age // 60}m ago"
            else:
                handshake_label = f"{age // 3600}h {(age % 3600) // 60}m ago"

        peers.append({
            "interface": iface,
            "pubkey": pubkey,
            "pubkey_short": pubkey[:8] + "…",
            "endpoint": endpoint,
            # Array form (legacy fetchWgPeerHealth) and string form (new health card)
            "allowed_ips": allowed_ips_list,
            "allowed_ips_str": allowed_ips_str,
            "latest_handshake": latest_handshake,
            "handshake_age": handshake_age,
            "handshake_label": handshake_label,
            # New health fields (for fetchWgPeers card)
            "last_handshake_age_sec": age_sec,
            "last_handshake_age": age_human,
            "status": health_status,
            "rx_bytes": rx_bytes,
            "rx_label": _fmt_bytes(rx_bytes),
            "rx_human": _fmt_bytes(rx_bytes),
            "tx_bytes": tx_bytes,
            "tx_label": _fmt_bytes(tx_bytes),
            "tx_human": _fmt_bytes(tx_bytes),
        })
    return jsonify({"peers": peers, "count": len(peers), "wg_available": True})


# ── Network Interfaces Overview ───────────────────────────────────────────────

@app.route("/api/network/interfaces", methods=["GET"])
@require_auth
def api_network_interfaces():
    """Return all network interfaces with addresses and traffic stats via ip."""
    import json as _json

    interfaces = []

    # Get address info (JSON output)
    addr_out, addr_rc = _run(["ip", "-j", "addr"])
    if addr_rc == 0:
        try:
            addr_data = _json.loads(addr_out)
        except ValueError:
            addr_data = []
    else:
        addr_data = []

    # Get stats info (JSON output)
    stats_out, stats_rc = _run(["ip", "-j", "-s", "link"])
    stats_map = {}
    if stats_rc == 0:
        try:
            stats_data = _json.loads(stats_out)
            for iface in stats_data:
                name = iface.get("ifname", "")
                stats = iface.get("stats64") or iface.get("stats") or {}
                rx = stats.get("rx", {})
                tx = stats.get("tx", {})
                stats_map[name] = {
                    "rx_bytes": rx.get("bytes", 0),
                    "tx_bytes": tx.get("bytes", 0),
                    "rx_errors": rx.get("errors", 0),
                    "tx_errors": tx.get("errors", 0),
                }
        except ValueError:
            pass

    def fmt_bytes(b):
        b = int(b)
        if b < 1024:
            return f"{b} B"
        elif b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        elif b < 1024 * 1024 * 1024:
            return f"{b / (1024*1024):.1f} MB"
        else:
            return f"{b / (1024*1024*1024):.2f} GB"

    for iface in addr_data:
        name = iface.get("ifname", "")
        flags = iface.get("flags", [])
        operstate = iface.get("operstate", "UNKNOWN").lower()
        link_type = iface.get("link_type", "")
        mac = iface.get("address", "")

        # Collect IP addresses
        addrs = []
        for addr_info in iface.get("addr_info", []):
            family = addr_info.get("family", "")
            local = addr_info.get("local", "")
            prefixlen = addr_info.get("prefixlen", "")
            if local:
                addrs.append({"family": family, "address": f"{local}/{prefixlen}"})

        # Traffic stats
        stats = stats_map.get(name, {})
        rx_bytes = stats.get("rx_bytes", 0)
        tx_bytes = stats.get("tx_bytes", 0)

        interfaces.append({
            "name": name,
            "operstate": operstate,
            "flags": flags,
            "link_type": link_type,
            "mac": mac,
            "addresses": addrs,
            "rx_bytes": rx_bytes,
            "rx_label": fmt_bytes(rx_bytes),
            "tx_bytes": tx_bytes,
            "tx_label": fmt_bytes(tx_bytes),
        })

    # Sort: up interfaces first, then by name
    interfaces.sort(key=lambda x: (0 if x["operstate"] == "up" else 1, x["name"]))

    return jsonify({"interfaces": interfaces, "count": len(interfaces)})


# ── AdGuard Home Stats ────────────────────────────────────────────────────────

@app.route("/api/dns/adguard/stats", methods=["GET"])
@require_auth
def api_dns_adguard_stats():
    """Return AdGuard Home query stats via its REST API."""
    import urllib.request
    import urllib.error
    import json as _json
    import base64

    # AdGuard Home listens on port 3000 (or 80 in some installs) on localhost
    # Try common ports
    adguard_base = None
    for port in [3000, 80, 8088]:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/control/status",
                headers={"User-Agent": "travel-router/1.0"},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    adguard_base = f"http://127.0.0.1:{port}"
                    break
        except Exception:
            continue

    if not adguard_base:
        return jsonify({"available": False, "error": "AdGuard Home not reachable"})

    def agh_get(path):
        try:
            req = urllib.request.Request(
                f"{adguard_base}{path}",
                headers={"User-Agent": "travel-router/1.0"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                return _json.loads(resp.read().decode())
        except Exception as e:
            return None

    stats = agh_get("/control/stats")
    if stats is None:
        return jsonify({"available": False, "error": "Failed to fetch AdGuard stats"})

    top_blocked = stats.get("top_blocked_domains", [])[:5]
    top_clients = stats.get("top_clients", [])[:5]
    top_queried = stats.get("top_queried_domains", [])[:5]

    num_dns_queries = stats.get("num_dns_queries", 0)
    num_blocked_filtering = stats.get("num_blocked_filtering", 0)
    num_replaced_safebrowsing = stats.get("num_replaced_safebrowsing", 0)
    num_replaced_parental = stats.get("num_replaced_parental", 0)
    total_blocked = num_blocked_filtering + num_replaced_safebrowsing + num_replaced_parental
    block_pct = round(total_blocked / num_dns_queries * 100, 1) if num_dns_queries > 0 else 0.0

    return jsonify({
        "available": True,
        "base_url": adguard_base,
        "num_dns_queries": num_dns_queries,
        "num_blocked_filtering": num_blocked_filtering,
        "total_blocked": total_blocked,
        "block_percent": block_pct,
        "avg_processing_time": stats.get("avg_processing_time", 0),
        "top_blocked_domains": top_blocked,
        "top_clients": top_clients,
        "top_queried_domains": top_queried,
    })


# ── System Services Status ────────────────────────────────────────────────────

TRAVEL_ROUTER_SERVICES = [
    "wg-quick@wg0",
    "hostapd",
    "dnsmasq",
    "adguardhome",
    "tailscaled",
    "travel-router-web",
    "wan-watchdog",
    "failover-watchdog",
    "wireguard-watchdog",
    "vnstat",
    "ntpd",
    "ssh",
]

@app.route("/api/system/services", methods=["GET"])
@require_auth
def api_system_services():
    """Return status of key travel-router systemd services."""
    services = []
    for svc in TRAVEL_ROUTER_SERVICES:
        out, rc = _run(["systemctl", "is-active", svc])
        active_state = out.strip() if out.strip() else "unknown"
        # is-active returns: active, inactive, activating, deactivating, failed, unknown
        enabled_out, enabled_rc = _run(["systemctl", "is-enabled", svc])
        enabled_state = enabled_out.strip() if enabled_out.strip() else "unknown"
        services.append({
            "name": svc,
            "active": active_state,
            "enabled": enabled_state,
            "running": active_state == "active",
        })
    return jsonify({"services": services, "count": len(services)})


# ── Tailscale Status ──────────────────────────────────────────────────────────

@app.route("/api/vpn/tailscale/status", methods=["GET"])
@require_auth
def api_vpn_tailscale_status():
    """Return Tailscale status: self node, peers, and connectivity."""
    import json as _json

    # Try tailscale status --json
    out, rc = _run(["tailscale", "status", "--json"])
    if rc != 0:
        return jsonify({"available": False, "error": "tailscale not installed or not running"})

    try:
        data = _json.loads(out)
    except ValueError:
        return jsonify({"available": False, "error": "failed to parse tailscale output"})

    # Extract self node info
    self_node = data.get("Self", {})
    self_info = {
        "hostname": self_node.get("HostName", ""),
        "dns_name": self_node.get("DNSName", "").rstrip("."),
        "tailscale_ips": self_node.get("TailscaleIPs", []),
        "os": self_node.get("OS", ""),
        "online": self_node.get("Online", False),
        "relay": self_node.get("Relay", ""),
    }

    # Extract peer info
    peers_raw = data.get("Peer", {})
    peers = []
    for _key, peer in peers_raw.items():
        last_seen = peer.get("LastSeen", "")
        # Active peers have LastHandshake or Active=true
        active = peer.get("Active", False)
        peers.append({
            "hostname": peer.get("HostName", ""),
            "dns_name": peer.get("DNSName", "").rstrip("."),
            "tailscale_ips": peer.get("TailscaleIPs", []),
            "os": peer.get("OS", ""),
            "online": peer.get("Online", False),
            "active": active,
            "relay": peer.get("Relay", ""),
            "rx_bytes": peer.get("RxBytes", 0),
            "tx_bytes": peer.get("TxBytes", 0),
            "last_seen": last_seen,
        })

    def fmt_bytes(b):
        b = int(b)
        if b < 1024:
            return f"{b} B"
        elif b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        elif b < 1024 * 1024 * 1024:
            return f"{b / (1024*1024):.1f} MB"
        else:
            return f"{b / (1024*1024*1024):.2f} GB"

    for p in peers:
        p["rx_label"] = fmt_bytes(p["rx_bytes"])
        p["tx_label"] = fmt_bytes(p["tx_bytes"])

    # Sort: online first, then by hostname
    peers.sort(key=lambda x: (0 if x["online"] else 1, x["hostname"]))

    backend_state = data.get("BackendState", "unknown")
    version = data.get("Version", "")

    return jsonify({
        "available": True,
        "backend_state": backend_state,
        "version": version,
        "self": self_info,
        "peers": peers,
        "peer_count": len(peers),
        "online_count": sum(1 for p in peers if p["online"]),
    })


# ── WAN Uplink Status ─────────────────────────────────────────────────────────

@app.route("/api/network/wan", methods=["GET"])
@require_auth
def api_network_wan():
    """Return WAN uplink status: all uplinks, active route, metrics."""
    import json as _json
    import re

    uplinks = []

    # Known WAN interface patterns
    wan_patterns = [
        ("wlan1", "WiFi Client", "wifi"),
        ("wlan2", "WiFi Client 2", "wifi"),
        ("usb0", "USB Tether", "usb"),
        ("bnep0", "Bluetooth Tether", "bluetooth"),
        ("wwan0", "LTE/Modem", "lte"),
        ("eth1", "Ethernet WAN", "ethernet"),
        ("ppp0", "PPP/Modem", "ppp"),
    ]

    # Get routing table to find default routes and metrics
    route_out, route_rc = _run(["ip", "-j", "route", "show", "default"])
    default_routes = {}
    if route_rc == 0:
        try:
            routes = _json.loads(route_out)
            for r in routes:
                dev = r.get("dev", "")
                metric = r.get("metric", 0)
                gateway = r.get("gateway", "")
                default_routes[dev] = {"metric": metric, "gateway": gateway}
        except ValueError:
            pass

    # Get interface states
    link_out, link_rc = _run(["ip", "-j", "link"])
    iface_states = {}
    if link_rc == 0:
        try:
            links = _json.loads(link_out)
            for link in links:
                name = link.get("ifname", "")
                operstate = link.get("operstate", "UNKNOWN").lower()
                iface_states[name] = operstate
        except ValueError:
            pass

    # Check config file for uplink priority order
    config_uplinks = []
    try:
        with open("/etc/default/travel-router") as f:
            for line in f:
                m = re.match(r'^UPLINK_ORDER="([^"]+)"', line.strip())
                if m:
                    config_uplinks = m.group(1).split()
    except OSError:
        pass

    for iface, label, iface_type in wan_patterns:
        state = iface_states.get(iface)
        if state is None:
            continue  # Interface doesn't exist

        route_info = default_routes.get(iface, {})
        has_route = iface in default_routes
        metric = route_info.get("metric", None)
        gateway = route_info.get("gateway", "")

        # Determine priority from config or route metric
        priority = config_uplinks.index(iface) + 1 if iface in config_uplinks else None

        uplinks.append({
            "interface": iface,
            "label": label,
            "type": iface_type,
            "state": state,
            "up": state == "up",
            "has_default_route": has_route,
            "metric": metric,
            "gateway": gateway,
            "priority": priority,
        })

    # Sort: active routes first (by metric), then up interfaces, then down
    def sort_key(u):
        if u["has_default_route"] and u["metric"] is not None:
            return (0, u["metric"])
        if u["up"]:
            return (1, 999)
        return (2, 999)

    uplinks.sort(key=sort_key)

    # Active uplink = lowest metric default route
    active_iface = None
    if uplinks:
        for u in uplinks:
            if u["has_default_route"]:
                active_iface = u["interface"]
                break

    return jsonify({
        "uplinks": uplinks,
        "count": len(uplinks),
        "active_interface": active_iface,
    })



# ── Speedtest ─────────────────────────────────────────────────────────────────

@app.route("/api/network/speedtest", methods=["GET"])
@require_auth
def api_network_speedtest():
    """Run a speedtest and return download/upload/ping results."""
    import json as _json
    import re

    # Try speedtest-cli (Python) first with --json
    out, rc = _run(["speedtest-cli", "--json", "--timeout", "30"])
    if rc == 0:
        try:
            d = _json.loads(out)
            download_mbps = round(d.get("download", 0) / 1_000_000, 2)
            upload_mbps = round(d.get("upload", 0) / 1_000_000, 2)
            ping_ms = round(d.get("ping", 0), 1)
            server = d.get("server", {})
            return jsonify({
                "available": True,
                "tool": "speedtest-cli",
                "download_mbps": download_mbps,
                "upload_mbps": upload_mbps,
                "ping_ms": ping_ms,
                "server_name": server.get("name", ""),
                "server_country": server.get("country", ""),
                "server_sponsor": server.get("sponsor", ""),
            })
        except (ValueError, KeyError):
            pass

    # Try speedtest (Ookla) with --format=json
    out, rc = _run(["speedtest", "--format=json", "--accept-license", "--accept-gdpr"])
    if rc == 0:
        try:
            d = _json.loads(out)
            dl = d.get("download", {})
            ul = d.get("upload", {})
            ping = d.get("ping", {})
            server = d.get("server", {})
            download_mbps = round(dl.get("bandwidth", 0) * 8 / 1_000_000, 2)
            upload_mbps = round(ul.get("bandwidth", 0) * 8 / 1_000_000, 2)
            ping_ms = round(ping.get("latency", 0), 1)
            return jsonify({
                "available": True,
                "tool": "speedtest-ookla",
                "download_mbps": download_mbps,
                "upload_mbps": upload_mbps,
                "ping_ms": ping_ms,
                "server_name": server.get("name", ""),
                "server_country": server.get("country", ""),
                "server_sponsor": server.get("host", ""),
            })
        except (ValueError, KeyError):
            pass

    # Try curl-based fallback: measure download from a known host
    out, rc = _run([
        "curl", "-o", "/dev/null", "-s", "-w", "%{speed_download}",
        "--max-time", "10",
        "https://speed.cloudflare.com/__down?bytes=10000000",
    ])
    if rc == 0:
        try:
            speed_bps = float(out.strip())
            download_mbps = round(speed_bps * 8 / 1_000_000, 2)
            return jsonify({
                "available": True,
                "tool": "curl",
                "download_mbps": download_mbps,
                "upload_mbps": None,
                "ping_ms": None,
                "server_name": "Cloudflare",
                "server_country": "",
                "server_sponsor": "speed.cloudflare.com",
            })
        except ValueError:
            pass

    return jsonify({"available": False, "error": "No speedtest tool available (install speedtest-cli)"})


# ── Temperature History ───────────────────────────────────────────────────────

import threading as _threading
import collections as _collections

_temp_history_lock = _threading.Lock()
_temp_history = _collections.deque(maxlen=60)  # 60 samples, 1/min = 1 hour

def _sample_temp():
    """Read current CPU temp and append to history ring buffer."""
    import time as _time
    temp = None
    # Try vcgencmd first (Pi-specific)
    out, rc = _run(["vcgencmd", "measure_temp"])
    if rc == 0:
        import re
        m = re.search(r"temp=([\d.]+)", out)
        if m:
            temp = float(m.group(1))
    if temp is None:
        # Fallback: sysfs thermal zone
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp = int(f.read().strip()) / 1000.0
        except OSError:
            pass
    if temp is not None:
        with _temp_history_lock:
            _temp_history.append({"ts": int(_time.time()), "temp": round(temp, 1)})

# Sample temperature every 60 seconds in a background daemon thread
def _start_temp_sampler():
    import time as _time
    import threading as _t
    def _loop():
        while True:
            try:
                _sample_temp()
            except Exception:
                pass
            _time.sleep(60)
    t = _t.Thread(target=_loop, daemon=True)
    t.start()

_start_temp_sampler()
_sample_temp()  # Take an immediate first sample

@app.route("/api/system/temp/history", methods=["GET"])
@require_auth
def api_system_temp_history():
    """Return CPU temperature history samples for the past hour."""
    with _temp_history_lock:
        samples = list(_temp_history)
    if not samples:
        return jsonify({"samples": [], "current": None, "min": None, "max": None, "avg": None})
    temps = [s["temp"] for s in samples]
    return jsonify({
        "samples": samples,
        "current": temps[-1],
        "min": min(temps),
        "max": max(temps),
        "avg": round(sum(temps) / len(temps), 1),
        "count": len(samples),
    })


# ── Log Export ────────────────────────────────────────────────────────────────

@app.route("/api/logs/export", methods=["GET"])
@require_auth
def api_logs_export():
    """Export combined system logs as a downloadable text file."""
    import io
    from flask import Response

    lines = []

    # journald: last 500 lines across all units
    out, rc = _run(["journalctl", "-n", "500", "--no-pager", "--output=short-iso"])
    if rc == 0 and out.strip():
        lines.append("=== journald (last 500 lines) ===")
        lines.append(out.rstrip())
        lines.append("")

    # Travel router specific service logs
    for service in ["travel-router-web", "wan-watchdog", "wg-quick@wg0", "tailscaled", "hostapd", "dnsmasq"]:
        svc_out, svc_rc = _run(["journalctl", "-u", service, "-n", "100", "--no-pager", "--output=short-iso"])
        if svc_rc == 0 and svc_out.strip():
            lines.append(f"=== {service} (last 100 lines) ===")
            lines.append(svc_out.rstrip())
            lines.append("")

    # Syslog if available
    for syslog_path in ["/var/log/syslog", "/var/log/messages"]:
        try:
            with open(syslog_path) as f:
                content = f.readlines()[-200:]
            lines.append(f"=== {syslog_path} (last 200 lines) ===")
            lines.append("".join(content).rstrip())
            lines.append("")
            break
        except OSError:
            continue

    content = "\n".join(lines) if lines else "No logs available.\n"

    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"travel-router-logs-{ts}.txt"

    return Response(
        content,
        mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/logs/summary", methods=["GET"])
@require_auth
def api_logs_summary():
    """Return a brief log summary: error/warning counts and recent errors."""
    import re

    summary = {"error_count": 0, "warn_count": 0, "recent_errors": [], "services_with_errors": []}

    out, rc = _run(["journalctl", "-n", "1000", "--no-pager", "--output=short-iso",
                    "-p", "0..4"])  # emerg..warning
    if rc == 0 and out.strip():
        for line in out.splitlines():
            lower = line.lower()
            if any(x in lower for x in [" err ", " error ", "failed", "crit", "alert", "emerg"]):
                summary["error_count"] += 1
                if len(summary["recent_errors"]) < 5:
                    summary["recent_errors"].append(line[:200])
            elif "warn" in lower:
                summary["warn_count"] += 1

    # Services with failures
    svc_out, svc_rc = _run(["systemctl", "list-units", "--state=failed", "--no-pager", "--plain"])
    if svc_rc == 0:
        for line in svc_out.splitlines():
            m = re.match(r'^\s*(\S+\.service)', line)
            if m:
                summary["services_with_errors"].append(m.group(1))

    return jsonify(summary)


# ── VPN Kill Switch ───────────────────────────────────────────────────────────

@app.route("/api/vpn/killswitch", methods=["GET"])
@require_auth
def api_vpn_killswitch():
    """Return kill switch status: whether traffic is blocked if VPN drops."""
    import re

    result = {
        "enabled": False,
        "method": None,
        "details": [],
        "vpn_interfaces": [],
    }

    # Detect WireGuard interfaces
    wg_out, wg_rc = _run(["wg", "show", "interfaces"])
    wg_ifaces = wg_out.strip().split() if wg_rc == 0 and wg_out.strip() else []
    result["vpn_interfaces"] = wg_ifaces

    # Check nftables for kill switch rules
    nft_out, nft_rc = _run(["nft", "list", "ruleset"])
    if nft_rc == 0:
        rules = nft_out
        # Kill switch typically: default drop policy + accept on wg/tun iface
        has_drop_forward = "drop" in rules and ("forward" in rules.lower() or "output" in rules.lower())
        has_wg_accept = any(iface in rules for iface in wg_ifaces) if wg_ifaces else False
        if has_drop_forward:
            result["enabled"] = True
            result["method"] = "nftables"
            result["details"].append("nftables: default drop policy detected")
            if has_wg_accept:
                result["details"].append(f"WireGuard interface(s) explicitly allowed: {', '.join(wg_ifaces)}")

    # Check iptables if nft not conclusive
    if not result["enabled"]:
        ipt_out, ipt_rc = _run(["iptables", "-L", "FORWARD", "-n"])
        if ipt_rc == 0:
            # Kill switch: FORWARD chain policy DROP
            if "policy DROP" in ipt_out or "Chain FORWARD (policy DROP)" in ipt_out:
                result["enabled"] = True
                result["method"] = "iptables"
                result["details"].append("iptables: FORWARD chain policy is DROP")
        ipt_out2, ipt_rc2 = _run(["iptables", "-L", "OUTPUT", "-n"])
        if ipt_rc2 == 0 and "policy DROP" in ipt_out2:
            result["enabled"] = True
            result["method"] = result["method"] or "iptables"
            result["details"].append("iptables: OUTPUT chain policy is DROP")

    # Check for common kill switch systemd service or config marker
    out3, rc3 = _run(["systemctl", "is-active", "travel-router-killswitch"])
    if rc3 == 0 and out3.strip() == "active":
        result["enabled"] = True
        result["method"] = result["method"] or "systemd"
        result["details"].append("systemd: travel-router-killswitch.service is active")

    # Check /etc/default/travel-router for KILL_SWITCH setting
    try:
        with open("/etc/default/travel-router") as f:
            for line in f:
                m = re.match(r'^KILL_SWITCH\s*=\s*["\']?(\w+)["\']?', line.strip())
                if m:
                    val = m.group(1).lower()
                    result["config_value"] = val
                    if val in ("1", "true", "yes", "on"):
                        result["details"].append(f"/etc/default/travel-router: KILL_SWITCH={val}")
    except OSError:
        pass

    return jsonify(result)


# ── Connected Clients ─────────────────────────────────────────────────────────

@app.route("/api/network/clients", methods=["GET"])
@require_auth
def api_network_clients():
    """Return connected LAN clients from ARP table and optionally nmap."""
    import re

    clients = []

    # Parse /proc/net/arp
    try:
        with open("/proc/net/arp") as f:
            lines = f.readlines()[1:]  # skip header
        for line in lines:
            parts = line.split()
            if len(parts) < 6:
                continue
            ip = parts[0]
            flags = parts[2]
            mac = parts[3]
            iface = parts[5].strip()
            # Skip incomplete entries (flags=0x0) and loopback
            if flags == "0x0" or mac == "00:00:00:00:00:00" or iface == "lo":
                continue
            # Try reverse DNS
            hostname = None
            out, rc = _run(["getent", "hosts", ip])
            if rc == 0 and out.strip():
                hostname = out.split()[1] if len(out.split()) > 1 else None
            clients.append({
                "ip": ip,
                "mac": mac,
                "hostname": hostname,
                "interface": iface,
                "vendor": _mac_vendor(mac),
            })
    except OSError:
        pass

    # Also check dnsmasq leases for hostnames we might have missed
    lease_names = {}
    lease_paths = ["/var/lib/misc/dnsmasq.leases", "/tmp/dnsmasq.leases", "/var/lib/dnsmasq/dnsmasq.leases"]
    for path in lease_paths:
        try:
            with open(path) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 4:
                        lease_mac = parts[1].lower()
                        lease_ip = parts[2]
                        lease_host = parts[3] if parts[3] != "*" else None
                        lease_names[lease_ip] = (lease_mac, lease_host)
            break
        except OSError:
            continue

    for c in clients:
        if c["hostname"] is None and c["ip"] in lease_names:
            c["hostname"] = lease_names[c["ip"]][1]

    # Sort by last octet of IP
    try:
        clients.sort(key=lambda c: int(c["ip"].split(".")[-1]))
    except (ValueError, IndexError):
        pass

    return jsonify({"clients": clients, "count": len(clients)})


def _mac_vendor(mac):
    """Return a short vendor hint from the MAC OUI (first 3 bytes)."""
    oui_map = {
        "b8:27:eb": "Raspberry Pi",
        "dc:a6:32": "Raspberry Pi",
        "e4:5f:01": "Raspberry Pi",
        "d8:3a:dd": "Raspberry Pi",
        "00:50:56": "VMware",
        "00:0c:29": "VMware",
        "08:00:27": "VirtualBox",
        "00:1a:11": "Google",
        "ac:37:43": "HTC",
        "f4:f5:d8": "Google",
        "04:d3:b0": "Apple",
        "a4:c3:f0": "Apple",
        "98:01:a7": "Apple",
        "3c:22:fb": "Apple",
        "00:1b:21": "Intel",
        "00:1e:65": "Intel",
        "18:66:da": "Intel",
    }
    prefix = mac.lower()[:8]
    return oui_map.get(prefix, None)


# ── WireGuard Peer Health ─────────────────────────────────────────────────────

def _format_duration(seconds):
    """Return a human-readable duration string from seconds."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    elif seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m"
    else:
        d = seconds // 86400
        h = (seconds % 86400) // 3600
        return f"{d}d {h}h"


def _fmt_bytes(n):
    """Return human-readable byte count."""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    else:
        return f"{n / 1024 / 1024 / 1024:.2f} GB"


# ── Data Cap Tracker ──────────────────────────────────────────────────────────

@app.route("/api/network/datacap", methods=["GET"])
@require_auth
def api_network_datacap():
    """Return current month's data usage from vnstat, plus configured cap."""
    import json
    import datetime

    # Read configured cap from /etc/default/travel-router (MONTHLY_CAP_GB=50)
    cap_gb = None
    try:
        with open("/etc/default/travel-router") as f:
            for line in f:
                line = line.strip()
                if line.startswith("MONTHLY_CAP_GB="):
                    val = line.split("=", 1)[1].strip().strip('"\'')
                    try:
                        cap_gb = float(val)
                    except ValueError:
                        pass
    except OSError:
        pass

    # Get month totals from vnstat
    out, rc = _run(["vnstat", "--json", "m"])
    if rc != 0:
        return jsonify({"error": "vnstat not available", "cap_gb": cap_gb})

    try:
        data = json.loads(out)
    except (ValueError, KeyError):
        return jsonify({"error": "Failed to parse vnstat output", "cap_gb": cap_gb})

    interfaces = data.get("interfaces", [])
    if not interfaces:
        return jsonify({"error": "No vnstat interfaces", "cap_gb": cap_gb})

    # Pick the primary interface (most total traffic)
    best = max(interfaces, key=lambda i: sum(
        e.get("rx", 0) + e.get("tx", 0)
        for e in i.get("traffic", {}).get("month", [])
    ))

    iface_name = best.get("name", "unknown")
    months = best.get("traffic", {}).get("month", [])

    # Current month
    now = datetime.datetime.now()
    current = None
    for m in months:
        d = m.get("date", {})
        if d.get("year") == now.year and d.get("month") == now.month:
            current = m
            break

    if not current:
        # Fall back to most recent
        if months:
            current = months[-1]

    rx_bytes = current.get("rx", 0) if current else 0
    tx_bytes = current.get("tx", 0) if current else 0
    total_bytes = rx_bytes + tx_bytes
    total_gb = total_bytes / 1024 / 1024 / 1024

    result = {
        "interface": iface_name,
        "rx_bytes": rx_bytes,
        "tx_bytes": tx_bytes,
        "total_bytes": total_bytes,
        "total_gb": round(total_gb, 3),
        "rx_gb": round(rx_bytes / 1024 / 1024 / 1024, 3),
        "tx_gb": round(tx_bytes / 1024 / 1024 / 1024, 3),
        "cap_gb": cap_gb,
        "percent_used": round((total_gb / cap_gb * 100), 1) if cap_gb else None,
        "remaining_gb": round(cap_gb - total_gb, 3) if cap_gb else None,
        "month": f"{now.year}-{now.month:02d}",
    }
    return jsonify(result)


# ── Network Interface Stats ───────────────────────────────────────────────────

@app.route("/api/network/iface/stats", methods=["GET"])
@require_auth
def api_network_iface_stats():
    """Return per-interface RX/TX stats including errors and drops from /proc/net/dev."""
    interfaces = []

    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()
    except OSError:
        return jsonify({"error": "Cannot read /proc/net/dev", "interfaces": []})

    # Skip first two header lines
    for line in lines[2:]:
        line = line.strip()
        if not line:
            continue
        colon = line.index(":")
        name = line[:colon].strip()
        fields = line[colon + 1:].split()
        if len(fields) < 16:
            continue
        # /proc/net/dev columns:
        # RX: bytes packets errs drop fifo frame compressed multicast
        # TX: bytes packets errs drop fifo colls carrier compressed
        rx_bytes = int(fields[0])
        rx_packets = int(fields[1])
        rx_errors = int(fields[2])
        rx_drop = int(fields[3])
        tx_bytes = int(fields[8])
        tx_packets = int(fields[9])
        tx_errors = int(fields[10])
        tx_drop = int(fields[11])
        tx_colls = int(fields[13])

        # Skip loopback and zero-traffic virtual interfaces (but keep wg, tun, eth, wlan, usb)
        if name == "lo":
            continue

        interfaces.append({
            "name": name,
            "rx_bytes": rx_bytes,
            "rx_packets": rx_packets,
            "rx_errors": rx_errors,
            "rx_drop": rx_drop,
            "tx_bytes": tx_bytes,
            "tx_packets": tx_packets,
            "tx_errors": tx_errors,
            "tx_drop": tx_drop,
            "tx_colls": tx_colls,
            "rx_human": _fmt_bytes(rx_bytes),
            "tx_human": _fmt_bytes(tx_bytes),
            "has_errors": (rx_errors + tx_errors + rx_drop + tx_drop + tx_colls) > 0,
        })

    # Sort: active interfaces first (most traffic), then by name
    interfaces.sort(key=lambda i: -(i["rx_bytes"] + i["tx_bytes"]))

    return jsonify({"interfaces": interfaces, "count": len(interfaces)})


# ── Tailscale Exit Node ───────────────────────────────────────────────────────

@app.route("/api/vpn/tailscale/exitnode", methods=["GET"])
@require_auth
def api_tailscale_exitnode():
    """Return current Tailscale exit node and list of available exit nodes."""
    import json

    out, rc = _run(["tailscale", "status", "--json"])
    if rc != 0:
        return jsonify({"error": "tailscale not available or not running", "current": None, "available": []})

    try:
        data = json.loads(out)
    except (ValueError, KeyError):
        return jsonify({"error": "Failed to parse tailscale status", "current": None, "available": []})

    # Find current exit node (ExitNodeStatus in Self)
    self_node = data.get("Self", {})
    current_exit = None

    # ExitNodeStatus is set when an exit node is in use
    exit_node_status = data.get("ExitNodeStatus")
    if exit_node_status:
        current_exit = {
            "tailscale_ip": exit_node_status.get("TailscaleIPs", [None])[0],
            "hostname": exit_node_status.get("HostName", ""),
            "dns_name": exit_node_status.get("DNSName", ""),
            "online": exit_node_status.get("Online", False),
        }

    # Collect all peers that advertise as exit nodes
    peers = data.get("Peer", {})
    available = []
    for node_id, peer in peers.items():
        # ExitNodeOption=True means it can be used as exit node
        if peer.get("ExitNodeOption", False):
            is_current = peer.get("ExitNode", False)
            available.append({
                "id": node_id,
                "hostname": peer.get("HostName", ""),
                "dns_name": peer.get("DNSName", ""),
                "tailscale_ip": peer.get("TailscaleIPs", [None])[0],
                "online": peer.get("Online", False),
                "current": is_current,
                "os": peer.get("OS", ""),
            })

    # Sort: current first, then online, then offline
    available.sort(key=lambda p: (not p["current"], not p["online"], p["hostname"]))

    # Is this device itself acting as an exit node?
    self_is_exit = self_node.get("ExitNodeOption", False)

    return jsonify({
        "current": current_exit,
        "available": available,
        "self_is_exit_node": self_is_exit,
        "self_hostname": self_node.get("HostName", ""),
    })


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=False)
