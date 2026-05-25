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

@app.route("/api/network/firewall")
def api_network_firewall():
    """Active firewall rules from iptables/nftables."""
    result = {"backend": None, "chains": [], "summary": {}}
    try:
        # Try nftables first
        nft_out, nft_rc = _run("nft list ruleset 2>/dev/null")
        if nft_rc == 0 and nft_out.strip():
            result["backend"] = "nftables"
            # Count rules by parsing lines that don't start with table/chain/}
            rule_lines = [l.strip() for l in nft_out.splitlines()
                          if l.strip() and not l.strip().startswith(("#", "table", "chain", "}", "{", "type", "hook", "policy"))]
            result["summary"]["total_rules"] = len(rule_lines)
            # Extract chain names
            chains = []
            current_chain = None
            for line in nft_out.splitlines():
                line = line.strip()
                m = re.match(r'^chain\s+(\S+)\s*\{', line)
                if m:
                    current_chain = {"name": m.group(1), "rules": [], "policy": None}
                    chains.append(current_chain)
                elif current_chain is not None:
                    if line == "}":
                        current_chain = None
                    elif line.startswith("type "):
                        # "type filter hook input priority 0; policy drop;"
                        pol_m = re.search(r"policy\s+(\w+)", line)
                        if pol_m:
                            current_chain["policy"] = pol_m.group(1).upper()
                    elif line and not line.startswith("{"):
                        current_chain["rules"].append(line[:100])
            result["chains"] = [{"name": c["name"], "policy": c["policy"], "rule_count": len(c["rules"]), "rules": c["rules"][:10]} for c in chains]
            return jsonify(result)
    except Exception:
        pass

    try:
        # Fall back to iptables
        ipt_out, ipt_rc = _run("iptables -L -n --line-numbers 2>/dev/null")
        if ipt_rc == 0 and ipt_out.strip():
            result["backend"] = "iptables"
            chains = []
            current_chain = None
            total_rules = 0
            for line in ipt_out.splitlines():
                if line.startswith("Chain "):
                    # "Chain INPUT (policy ACCEPT)" or "Chain FORWARD (policy DROP)"
                    m = re.match(r"^Chain\s+(\S+)\s+\(policy\s+(\w+)", line)
                    if m:
                        current_chain = {"name": m.group(1), "policy": m.group(2), "rules": [], "rule_count": 0}
                        chains.append(current_chain)
                    else:
                        m2 = re.match(r"^Chain\s+(\S+)", line)
                        if m2:
                            current_chain = {"name": m2.group(1), "policy": None, "rules": [], "rule_count": 0}
                            chains.append(current_chain)
                elif current_chain is not None and line.strip() and not line.startswith("num ") and not line.startswith("target "):
                    # Skip header lines that start with "num " or "target "
                    if re.match(r'^\d+\s+', line.strip()):
                        current_chain["rules"].append(line.strip()[:100])
                        current_chain["rule_count"] += 1
                        total_rules += 1
            result["chains"] = chains
            result["summary"]["total_rules"] = total_rules
            return jsonify(result)
    except Exception as e:
        result["error"] = str(e)

    result["error"] = result.get("error", "No supported firewall backend found (nftables/iptables)")
    return jsonify(result)


# ── Bandwidth History ─────────────────────────────────────────────────────────

@app.route("/api/network/bandwidth/history", methods=["GET"])
@require_auth
def api_network_bandwidth_history():
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
def api_network_routes_v1():
    """Return IPv4 and IPv6 routing table (legacy text parser)."""
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


@app.route("/api/network/active-connections")
@require_auth
def api_network_active_connections():
    out, rc = _run(["ss", "-tnup", "state", "established"])
    connections = []
    if rc == 0:
        for line in out.splitlines()[1:]:  # skip header
            parts = line.split()
            if len(parts) < 5:
                continue
            conn = {
                "proto": parts[0],
                "local": parts[3],
                "remote": parts[4],
                "process": None,
            }
            # extract process name from users:(("name",pid=N,fd=N))
            for p in parts[5:]:
                if p.startswith("users:"):
                    m = re.search(r'"([^"]+)"', p)
                    if m:
                        conn["process"] = m.group(1)
            connections.append(conn)
    return jsonify({"connections": connections, "total": len(connections)})


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
    interfaces = []

    # Get address info (JSON output)
    addr_out, addr_rc = _run(["ip", "-j", "addr"])
    if addr_rc == 0:
        try:
            addr_data = json.loads(addr_out)
        except ValueError:
            addr_data = []
    else:
        addr_data = []

    # Get stats info (JSON output)
    stats_out, stats_rc = _run(["ip", "-j", "-s", "link"])
    stats_map = {}
    if stats_rc == 0:
        try:
            stats_data = json.loads(stats_out)
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

@app.route("/api/system/services", methods=["GET"])
@require_auth
def api_system_services():
    """Return all systemd service units with load/active/sub state and description."""
    out, rc = _run(
        "systemctl list-units --type=service --no-pager --no-legend --all",
        timeout=10,
    )
    services = []
    if rc == 0 and out:
        for line in out.splitlines():
            # Strip leading whitespace and bullet characters systemctl may emit
            line = line.strip()
            if not line:
                continue
            # Remove non-ASCII bullet prefix (e.g. ● or similar)
            if line and not line[0].isascii():
                line = line.lstrip()
                # drop the first non-ASCII char
                line = line[1:].lstrip()
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            name, load, active, sub = parts[0], parts[1], parts[2], parts[3]
            description = parts[4] if len(parts) >= 5 else ""
            services.append({
                "name": name,
                "load": load,
                "active": active,
                "sub": sub,
                "description": description,
            })
    total = len(services)
    active_count = sum(1 for s in services if s["active"] == "active")
    failed_count = sum(1 for s in services if s["active"] == "failed")
    inactive_count = sum(1 for s in services if s["active"] == "inactive")
    # Sort: failed first, then active, then others
    services.sort(key=lambda s: (0 if s["active"] == "failed" else 1 if s["active"] == "active" else 2, s["name"]))
    summary = {
        "total": total,
        "active": active_count,
        "failed": failed_count,
        "inactive": inactive_count,
    }
    return jsonify({
        "services": services[:50],
        "summary": summary,
        "total": total,
        "active": active_count,
        "failed": failed_count,
    })


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


# ── Cron Jobs Viewer ──────────────────────────────────────────────────────────

@app.route("/api/system/cron", methods=["GET"])
@require_auth
def api_system_cron():
    """Return cron jobs from system crontabs and /etc/cron.d/."""
    import os
    import re

    jobs = []

    def parse_crontab(content, source):
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Skip environment variable lines (KEY=value)
            if re.match(r'^[A-Z_]+=', line):
                continue
            parts = line.split(None, 5)
            # Standard crontab: min hour dom month dow command
            # /etc/cron.d also has a user field: min hour dom month dow user command
            if len(parts) >= 6:
                schedule = " ".join(parts[:5])
                # Detect if this is a /etc/cron.d entry (has user field)
                if source.startswith("/etc/cron.d/"):
                    user = parts[5] if len(parts) > 6 else ""
                    command = parts[6] if len(parts) > 6 else parts[5]
                else:
                    user = ""
                    command = parts[5]
                jobs.append({
                    "schedule": schedule,
                    "user": user,
                    "command": command[:120],  # truncate long commands
                    "source": source,
                })

    # System crontab
    try:
        with open("/etc/crontab") as f:
            parse_crontab(f.read(), "/etc/crontab")
    except OSError:
        pass

    # /etc/cron.d/
    try:
        cron_d = "/etc/cron.d"
        for fname in sorted(os.listdir(cron_d)):
            fpath = os.path.join(cron_d, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath) as f:
                        parse_crontab(f.read(), fpath)
                except OSError:
                    pass
    except OSError:
        pass

    # Root user crontab (crontab -l -u root)
    out, rc = _run(["crontab", "-l", "-u", "root"])
    if rc == 0 and out.strip() and "no crontab for" not in out:
        parse_crontab(out, "root crontab")

    # travel-router user crontab if it exists
    out2, rc2 = _run(["crontab", "-l", "-u", "travel-router"])
    if rc2 == 0 and out2.strip() and "no crontab for" not in out2:
        parse_crontab(out2, "travel-router crontab")

    return jsonify({"jobs": jobs, "count": len(jobs)})


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


# ── Top Processes ─────────────────────────────────────────────────────────────

@app.route("/api/system/proctop", methods=["GET"])
@require_auth
def api_proctop():
    """Return top 10 processes by CPU% and top 10 by MEM%."""
    out, rc = _run(["ps", "aux", "--no-header"])
    if rc != 0:
        return jsonify({"error": "ps command failed", "by_cpu": [], "by_mem": [], "total_processes": 0})

    processes = []
    for line in out.splitlines():
        cols = line.split(None, 10)
        if len(cols) < 11:
            continue
        try:
            name = os.path.basename(cols[10].split()[0]) if cols[10].split() else ""
        except (IndexError, AttributeError):
            name = ""
        if not name or name == "-":
            continue
        try:
            proc = {
                "pid": int(cols[1]),
                "name": name,
                "cpu_pct": float(cols[2]),
                "mem_pct": float(cols[3]),
                "state": cols[7],
                "user": cols[0],
            }
        except (ValueError, IndexError):
            continue
        processes.append(proc)

    by_cpu = sorted(processes, key=lambda p: p["cpu_pct"], reverse=True)[:10]
    by_mem = sorted(processes, key=lambda p: p["mem_pct"], reverse=True)[:10]

    return jsonify({
        "by_cpu": by_cpu,
        "by_mem": by_mem,
        "total_processes": len(processes),
    })


# ── System Entropy ────────────────────────────────────────────────────────────

@app.route("/api/system/entropy")
@require_auth
def api_system_entropy():
    """Return kernel entropy pool stats and RNG health."""
    try:
        entropy_avail = int(Path("/proc/sys/kernel/random/entropy_avail").read_text().strip())
        pool_size = int(Path("/proc/sys/kernel/random/poolsize").read_text().strip())

        try:
            read_threshold = int(
                Path("/proc/sys/kernel/random/read_wakeup_threshold").read_text().strip()
            )
        except (OSError, ValueError):
            read_threshold = None

        try:
            write_threshold = int(
                Path("/proc/sys/kernel/random/write_wakeup_threshold").read_text().strip()
            )
        except (OSError, ValueError):
            write_threshold = None

        try:
            hw_rng = Path("/sys/class/misc/hw_random/rng_current").read_text().strip()
        except (OSError, ValueError):
            hw_rng = None

        try:
            hw_rng_available = (
                Path("/sys/class/misc/hw_random/rng_available").read_text().strip().split()
            )
        except (OSError, ValueError):
            hw_rng_available = []

        try:
            import subprocess as _sp
            uuid_raw = _sp.check_output(
                ["cat", "/proc/sys/kernel/random/uuid"],
                stderr=_sp.DEVNULL,
                timeout=2,
            ).decode().strip()
            uuid_ok = len(uuid_raw) == 36
        except Exception:
            uuid_ok = False

        pct = round(
            min(max(entropy_avail / pool_size * 100, 0), 100), 2
        ) if pool_size else 0.0

        return jsonify({
            "entropy_avail": entropy_avail,
            "pool_size": pool_size,
            "pct": pct,
            "read_threshold": read_threshold,
            "write_threshold": write_threshold,
            "hw_rng": hw_rng,
            "hw_rng_available": hw_rng_available,
            "uuid_ok": uuid_ok,
            "error": None,
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "entropy_avail": 0, "pool_size": 4096})


# ── USB Device Inventory ──────────────────────────────────────────────────────

@app.route("/api/system/usb", methods=["GET"])
@require_auth
def api_system_usb():
    """Return a list of connected USB devices with speed info, hubs last."""
    import re as _re

    devices = []
    error = None

    out, rc = _run(["lsusb"])
    if rc == 0 and out:
        for line in out.splitlines():
            m = _re.match(
                r"Bus (\d+) Device (\d+): ID ([0-9a-f]{4}):([0-9a-f]{4})\s+(.*)",
                line,
            )
            if m:
                devices.append(
                    {
                        "bus": m.group(1),
                        "device": m.group(2),
                        "vendor_id": m.group(3),
                        "product_id": m.group(4),
                        "description": m.group(5).strip(),
                        "speed_mbps": None,
                    }
                )
    else:
        # Fallback: parse /sys/kernel/debug/usb/devices
        try:
            with open("/sys/kernel/debug/usb/devices") as fh:
                content = fh.read()
            cur: dict = {}
            for line in content.splitlines():
                if line.startswith("T:"):
                    if cur.get("vendor_id"):
                        devices.append(cur)
                    cur = {"bus": "", "device": "", "vendor_id": "0000",
                           "product_id": "0000", "description": "", "speed_mbps": None}
                    bm = _re.search(r"Bus=(\d+)", line)
                    dm = _re.search(r"Dev#=\s*(\d+)", line)
                    sm = _re.search(r"Spd=([\d.]+)", line)
                    if bm:
                        cur["bus"] = bm.group(1).zfill(3)
                    if dm:
                        cur["device"] = dm.group(1).zfill(3)
                    if sm:
                        cur["speed_mbps"] = sm.group(1)
                elif line.startswith("P:"):
                    pm = _re.search(r"Vendor=([0-9a-fA-F]{4})\s+ProdID=([0-9a-fA-F]{4})", line)
                    if pm:
                        cur["vendor_id"] = pm.group(1).lower()
                        cur["product_id"] = pm.group(2).lower()
                elif line.startswith("S:") and "Product=" in line:
                    cur["description"] = line.split("Product=", 1)[1].strip()
            if cur.get("vendor_id") and cur["vendor_id"] != "0000":
                devices.append(cur)
        except OSError:
            error = "lsusb not found and /sys/kernel/debug/usb/devices unavailable"

    # Enrich with speed from /sys/bus/usb/devices/
    sys_usb = Path("/sys/bus/usb/devices")
    speed_map: dict = {}
    if sys_usb.is_dir():
        for entry in sys_usb.iterdir():
            try:
                busnum_file = entry / "busnum"
                devnum_file = entry / "devnum"
                speed_file = entry / "speed"
                if busnum_file.exists() and devnum_file.exists() and speed_file.exists():
                    bus = busnum_file.read_text().strip().zfill(3)
                    dev = devnum_file.read_text().strip().zfill(3)
                    speed = speed_file.read_text().strip()
                    speed_map[(bus, dev)] = speed
            except OSError:
                continue

    for dev in devices:
        key = (dev["bus"].zfill(3), dev["device"].zfill(3))
        if key in speed_map and dev["speed_mbps"] is None:
            dev["speed_mbps"] = speed_map[key]

    # Sort: hubs last, then by bus+device
    devices.sort(key=lambda d: (
        1 if "hub" in d["description"].lower() else 0,
        d["bus"].zfill(3),
        d["device"].zfill(3),
    ))

    result: dict = {"devices": devices, "count": len(devices)}
    if error:
        result["error"] = error
    return jsonify(result)


# ── System Temperature Details ────────────────────────────────────────────────

@app.route("/api/system/temps", methods=["GET"])
@require_auth
def api_system_temps():
    """Return thermal zone temperatures, vcgencmd data, and throttle flags."""
    import glob as _glob

    sensors = []

    # Read all thermal zones from sysfs
    zone_temps = sorted(_glob.glob("/sys/class/thermal/thermal_zone*/temp"))
    for temp_path in zone_temps:
        zone_dir = temp_path.rsplit("/temp", 1)[0]
        # Read temperature (millidegrees → degrees)
        try:
            temp_c = int(Path(temp_path).read_text().strip()) / 1000.0
        except (OSError, ValueError):
            continue

        # Read zone type (friendly name)
        try:
            zone_type = Path(zone_dir + "/type").read_text().strip()
        except OSError:
            zone_type = zone_dir.split("/")[-1]

        # Try to read critical trip point (trip_point_0_temp)
        critical_c = None
        try:
            crit_raw = int(Path(zone_dir + "/trip_point_0_temp").read_text().strip())
            critical_c = crit_raw / 1000.0
        except (OSError, ValueError):
            pass

        # Classify status
        if temp_c < 70.0:
            status = "ok"
        elif temp_c <= 80.0:
            status = "warm"
        else:
            status = "hot"

        sensors.append({
            "name": zone_type,
            "label": zone_type.replace("_", " ").replace("-", " ").title(),
            "temp_c": round(temp_c, 1),
            "critical": round(critical_c, 1) if critical_c is not None else None,
            "status": status,
        })

    # vcgencmd measure_temp (Pi-specific)
    vcg_temp = None
    out, rc = _run(["vcgencmd", "measure_temp"])
    if rc == 0:
        import re as _re
        m = _re.search(r"temp=([\d.]+)", out)
        if m:
            vcg_temp = float(m.group(1))
            # Add as a sensor if not already represented
            if not any(s["name"] == "gpu_thermal" for s in sensors):
                t = vcg_temp
                status = "ok" if t < 70.0 else ("warm" if t <= 80.0 else "hot")
                sensors.append({
                    "name": "gpu_thermal",
                    "label": "GPU",
                    "temp_c": round(t, 1),
                    "critical": None,
                    "status": status,
                })

    # vcgencmd get_throttled
    throttled = False
    throttle_flags = "0x0"
    out2, rc2 = _run(["vcgencmd", "get_throttled"])
    if rc2 == 0:
        import re as _re2
        m2 = _re2.search(r"throttled=(0x[0-9a-fA-F]+)", out2)
        if m2:
            throttle_flags = m2.group(1)
            throttled = int(throttle_flags, 16) != 0

    max_temp = None
    if sensors:
        max_temp = max(s["temp_c"] for s in sensors)

    return jsonify({
        "sensors": sensors,
        "max_temp_c": max_temp,
        "throttled": throttled,
        "throttle_flags": throttle_flags,
    })


# ── WireGuard Config Export ────────────────────────────────────────────────────


@app.route("/api/vpn/wireguard/config/qr", methods=["GET"])
@require_auth
def api_wireguard_config_qr():
    """Generate a QR code of the WireGuard client config for mobile import."""
    import subprocess
    import tempfile
    import os

    conf_path = "/etc/wireguard/wg0.conf"
    try:
        with open(conf_path) as f:
            conf_text = f.read()
    except OSError as e:
        return jsonify({"error": f"Cannot read {conf_path}: {e}"}), 500

    qr_out, qr_rc = _run(["which", "qrencode"])
    if qr_rc != 0:
        return jsonify({"error": "qrencode not installed"}), 500

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["qrencode", "-o", tmp_path, "-t", "PNG", "--dpi", "150"],
            input=conf_text.encode(),
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return jsonify({"error": "qrencode failed"}), 500

        with open(tmp_path, "rb") as f:
            png_data = f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    from flask import Response
    return Response(
        png_data,
        mimetype="image/png",
        headers={
            "Content-Disposition": "inline; filename=wg0-qr.png",
            "Cache-Control": "no-store",
        },
    )


@app.route("/api/vpn/wireguard/config/text", methods=["GET"])
@require_auth
def api_wireguard_config_text():
    """Return the WireGuard config file text (sanitized — private key redacted)."""
    conf_path = "/etc/wireguard/wg0.conf"
    try:
        with open(conf_path) as f:
            lines = f.readlines()
    except OSError as e:
        return jsonify({"error": f"Cannot read {conf_path}: {e}"}), 500

    sanitized = []
    for line in lines:
        if line.strip().lower().startswith("privatekey"):
            sanitized.append("PrivateKey = <redacted>\n")
        else:
            sanitized.append(line)

    return jsonify({"config": "".join(sanitized), "path": conf_path})


# ── Kernel Modules ────────────────────────────────────────────────────────────

@app.route("/api/system/modules", methods=["GET"])
@require_auth
def api_system_modules():
    modules = []
    try:
        with open("/proc/modules", "r") as fh:
            for line in fh:
                cols = line.split()
                if len(cols) < 4:
                    continue
                name = cols[0]
                try:
                    size = int(cols[1])
                except ValueError:
                    size = 0
                try:
                    used_by = int(cols[2])
                except ValueError:
                    used_by = 0
                raw_deps = cols[3].strip(",")
                deps = raw_deps.split(",") if raw_deps != "-" else []
                modules.append({
                    "name": name,
                    "size": size,
                    "used_by": used_by,
                    "deps": deps,
                })
    except OSError as exc:
        return jsonify({"error": str(exc)}), 503
    modules.sort(key=lambda m: m["name"])
    return jsonify({"modules": modules, "count": len(modules)})


# ── Disk I/O Stats ────────────────────────────────────────────────────────────

@app.route("/api/system/diskio", methods=["GET"])
@require_auth
def api_system_diskio():
    """Parse /proc/diskstats for block devices (exclude loop/ram devices)."""
    devices = []
    try:
        with open("/proc/diskstats", "r") as fh:
            for line in fh:
                cols = line.split()
                if len(cols) < 14:
                    continue
                dev = cols[2]
                # Skip loop, ram, and partition entries (sdXN, mmcblk0pN)
                if dev.startswith(("loop", "ram")):
                    continue
                # Skip partitions (have a digit at end after a letter)
                if re.search(r"[a-z]\d+$", dev):
                    continue
                try:
                    reads_completed   = int(cols[3])
                    reads_merged      = int(cols[4])
                    sectors_read      = int(cols[5])
                    read_ms           = int(cols[6])
                    writes_completed  = int(cols[7])
                    writes_merged     = int(cols[8])
                    sectors_written   = int(cols[9])
                    write_ms          = int(cols[10])
                    io_in_progress    = int(cols[11])
                    io_ms             = int(cols[12])
                except (ValueError, IndexError):
                    continue
                devices.append({
                    "device":           dev,
                    "reads_completed":  reads_completed,
                    "reads_merged":     reads_merged,
                    "sectors_read":     sectors_read,
                    "bytes_read":       sectors_read * 512,
                    "read_ms":          read_ms,
                    "writes_completed": writes_completed,
                    "writes_merged":    writes_merged,
                    "sectors_written":  sectors_written,
                    "bytes_written":    sectors_written * 512,
                    "write_ms":         write_ms,
                    "io_in_progress":   io_in_progress,
                    "io_ms":            io_ms,
                })
    except OSError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"devices": devices})


# ── Memory Info ───────────────────────────────────────────────────────────────

@app.route("/api/system/meminfo", methods=["GET"])
@require_auth
def api_system_meminfo():
    raw: dict = {}
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    try:
                        raw[key] = int(parts[1])
                    except ValueError:
                        raw[key] = 0
    except OSError as exc:
        return jsonify({"error": str(exc), "raw": {}}), 503

    def _mb(key: str) -> float:
        return round(raw.get(key, 0) / 1024, 2)

    mem_total = raw.get("MemTotal", 0)
    mem_avail = raw.get("MemAvailable", raw.get("MemFree", 0))
    used_kb = mem_total - mem_avail
    used_pct = (used_kb / mem_total * 100) if mem_total > 0 else 0.0

    swap_total = raw.get("SwapTotal", 0)
    swap_free = raw.get("SwapFree", 0)
    swap_used = swap_total - swap_free
    swap_pct = (swap_used / swap_total * 100) if swap_total > 0 else 0.0

    result = {
        "total_mb":       round(mem_total / 1024, 2),
        "free_mb":        _mb("MemFree"),
        "available_mb":   round(mem_avail / 1024, 2),
        "used_mb":        round(used_kb / 1024, 2),
        "used_pct":       round(used_pct, 1),
        "buffers_mb":     _mb("Buffers"),
        "cached_mb":      _mb("Cached"),
        "swap_total_mb":  round(swap_total / 1024, 2),
        "swap_free_mb":   round(swap_free / 1024, 2),
        "swap_used_mb":   round(swap_used / 1024, 2),
        "swap_pct":       round(swap_pct, 1),
        "dirty_mb":       _mb("Dirty"),
        "anon_pages_mb":  _mb("AnonPages"),
        "shmem_mb":       _mb("Shmem"),
        "hugepages_total": raw.get("HugePages_Total", 0),
        "hugepages_free":  raw.get("HugePages_Free", 0),
        "raw":            raw,
        "error":          None,
    }
    return jsonify(result)


# ── NTP / Time Sync Status ────────────────────────────────────────────────────

@app.route("/api/system/ntp", methods=["GET"])
@require_auth
def api_system_ntp():
    """Return NTP/time sync status via timedatectl and chronyc."""
    _CHRONY_TRACKING_KEYS = {
        "Reference ID", "Stratum", "System time", "RMS offset",
        "Frequency", "Last offset", "Root delay", "Root dispersion",
    }
    result = {
        "synchronized": False,
        "ntp_service": "systemd-timesyncd",
        "timedatectl": {},
        "chrony": {"tracking": {}, "sources_raw": ""},
        "error": None,
    }

    # timedatectl show --no-pager → key=value pairs
    try:
        out, rc = _run(["timedatectl", "show", "--no-pager"])
        if rc == 0 and out:
            td = {}
            for line in out.splitlines():
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                td[k.strip()] = v.strip()
            result["timedatectl"] = td
            result["synchronized"] = td.get("NTPSynchronized", "") == "yes"
            if td.get("NTPService"):
                result["ntp_service"] = td["NTPService"]
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)

    # chronyc tracking
    try:
        out2, rc2 = _run(["chronyc", "tracking"])
        if rc2 == 0 and out2:
            result["ntp_service"] = "chrony"
            tracking = {}
            for line in out2.splitlines():
                line = line.strip()
                for key in _CHRONY_TRACKING_KEYS:
                    if line.startswith(key):
                        parts = line.split(":", 1)
                        if len(parts) > 1:
                            tracking[key] = parts[1].strip()
                        break
            result["chrony"]["tracking"] = tracking
    except Exception:  # noqa: BLE001
        pass

    # chronyc sources -v (first 30 lines)
    try:
        out3, rc3 = _run(["chronyc", "sources", "-v"])
        if rc3 == 0 and out3:
            lines = out3.splitlines()[:30]
            result["chrony"]["sources_raw"] = "\n".join(lines)
    except Exception:  # noqa: BLE001
        pass

    return jsonify(result)


# ── Open File Descriptors ─────────────────────────────────────────────────────

@app.route("/api/system/openfiles", methods=["GET"])
@require_auth
def api_system_openfiles():
    """Return open file descriptor stats from /proc/sys/fs/file-nr and top procs."""
    result = {
        "allocated": 0,
        "free": 0,
        "max": 0,
        "pct_used": 0.0,
        "top_procs": [],
    }
    # System-wide fd counts from /proc/sys/fs/file-nr
    # Format: allocated  free  max
    try:
        line = open("/proc/sys/fs/file-nr").read().strip()
        parts = line.split()
        if len(parts) >= 3:
            result["allocated"] = int(parts[0])
            result["free"]      = int(parts[1])
            result["max"]       = int(parts[2])
            if result["max"] > 0:
                result["pct_used"] = round(result["allocated"] / result["max"] * 100, 1)
    except (OSError, ValueError):
        pass

    # Per-process fd counts — top 10 by open fd count
    import os as _os
    proc_fds = []
    try:
        for pid_str in _os.listdir("/proc"):
            if not pid_str.isdigit():
                continue
            fd_dir = f"/proc/{pid_str}/fd"
            try:
                fd_count = len(_os.listdir(fd_dir))
            except OSError:
                continue
            try:
                comm = open(f"/proc/{pid_str}/comm").read().strip()
            except OSError:
                comm = pid_str
            proc_fds.append({"pid": int(pid_str), "comm": comm, "fds": fd_count})
    except OSError:
        pass

    proc_fds.sort(key=lambda x: x["fds"], reverse=True)
    result["top_procs"] = proc_fds[:10]
    return jsonify(result)


# ── CPU Frequency ─────────────────────────────────────────────────────────────

@app.route("/api/system/cpufreq", methods=["GET"])
@require_auth
def api_system_cpufreq():
    """Return per-core CPU frequency info from sysfs (or /proc/cpuinfo fallback)."""
    import glob as _glob

    def _read_khz(path):
        try:
            return round(int(Path(path).read_text().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            return None

    def _read_str(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None

    cores = []
    cpu_dirs = sorted(_glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"))
    for cpu_dir in cpu_dirs:
        cpu_id_str = cpu_dir.split("/")[-2]
        try:
            cpu_id = int(cpu_id_str.replace("cpu", ""))
        except ValueError:
            continue
        cur = _read_khz(f"{cpu_dir}/scaling_cur_freq")
        if cur is None:
            cur = _read_khz(f"{cpu_dir}/cpuinfo_cur_freq")
        cores.append({
            "id": cpu_id,
            "cur_mhz": cur,
            "min_mhz": _read_khz(f"{cpu_dir}/scaling_min_freq"),
            "max_mhz": _read_khz(f"{cpu_dir}/scaling_max_freq"),
        })

    # Fallback: parse /proc/cpuinfo if sysfs cpufreq not available
    if not cores:
        import re as _re
        try:
            text = Path("/proc/cpuinfo").read_text()
            mhz_vals = [float(m.group(1)) for m in _re.finditer(r"cpu MHz\s*:\s*([\d.]+)", text)]
            for idx, mhz in enumerate(mhz_vals):
                cores.append({"id": idx, "cur_mhz": round(mhz, 1), "min_mhz": None, "max_mhz": None})
        except Exception:
            pass

    if not cores:
        return jsonify({"cores": [], "governor": None, "avg_mhz": None,
                        "error": "cpufreq data not available"})

    governor = _read_str("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    cur_vals = [c["cur_mhz"] for c in cores if c["cur_mhz"] is not None]
    avg_mhz = round(sum(cur_vals) / len(cur_vals), 1) if cur_vals else None
    return jsonify({"cores": cores, "governor": governor, "avg_mhz": avg_mhz})


# ── ARP / Neighbor Table ──────────────────────────────────────────────────────

@app.route("/api/network/arp", methods=["GET"])
@require_auth
def api_network_arp():
    """Return ARP/NDP neighbor table using `ip neigh show` (IPv4+IPv6) with fallback to /proc/net/arp."""
    import ipaddress

    def _classify(ip_str):
        try:
            return isinstance(ipaddress.ip_address(ip_str), ipaddress.IPv6Address)
        except ValueError:
            return False

    def _sort_key(n):
        order = 0 if n["state"] == "REACHABLE" else 1
        try:
            packed = ipaddress.ip_address(n["ip"]).packed
        except ValueError:
            packed = b""
        return (order, packed)

    neighbors = []
    try:
        out, rc = _run(["ip", "neigh", "show"], timeout=5)
        if rc == 0 and out.strip():
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                # format: IP dev IFACE [lladdr MAC] [PROBES N] STATE
                if len(parts) < 4:
                    continue
                ip = parts[0]
                iface = parts[2] if len(parts) > 2 else ""
                mac = None
                state = parts[-1].upper()
                if "lladdr" in parts:
                    idx = parts.index("lladdr")
                    if idx + 1 < len(parts):
                        mac = parts[idx + 1]
                if state in ("FAILED", "INCOMPLETE"):
                    neighbors.append({
                        "ip": ip,
                        "mac": mac,
                        "iface": iface,
                        "state": state,
                        "is_ipv6": _classify(ip),
                    })
                    continue
                neighbors.append({
                    "ip": ip,
                    "mac": mac,
                    "iface": iface,
                    "state": state,
                    "is_ipv6": _classify(ip),
                })
        else:
            # Fallback: /proc/net/arp (IPv4 only)
            _FLAG_MAP = {"0x0": "INCOMPLETE", "0x2": "REACHABLE", "0x4": "STALE", "0x6": "STALE"}
            try:
                with open("/proc/net/arp") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("IP address"):
                            continue
                        parts = line.split()
                        if len(parts) < 6:
                            continue
                        ip, flags, mac, iface = parts[0], parts[2], parts[3], parts[5]
                        if mac == "00:00:00:00:00:00":
                            continue
                        neighbors.append({
                            "ip": ip,
                            "mac": mac,
                            "iface": iface,
                            "state": _FLAG_MAP.get(flags.lower(), "UNKNOWN"),
                            "is_ipv6": False,
                        })
            except OSError:
                pass
    except Exception as exc:
        return jsonify({"neighbors": [], "count": 0, "reachable": 0, "stale": 0, "error": str(exc)})

    # Filter FAILED/INCOMPLETE unless no others exist
    visible = [n for n in neighbors if n["state"] not in ("FAILED", "INCOMPLETE")]
    if not visible:
        visible = neighbors
    visible.sort(key=_sort_key)
    reachable = sum(1 for n in visible if n["state"] == "REACHABLE")
    stale = sum(1 for n in visible if n["state"] in ("STALE", "DELAY", "PROBE"))
    return jsonify({"neighbors": visible, "count": len(visible), "reachable": reachable, "stale": stale})


# ── IP Routing Table ──────────────────────────────────────────────────────────

@app.route("/api/network/routes", methods=["GET"])
@require_auth
def api_network_routes():
    """Return the main IPv4 routing table parsed from `ip -j route show`."""
    try:
        out, rc = _run(["ip", "-j", "route", "show"])
        if rc != 0:
            return jsonify({"error": out.strip() or "ip route show failed", "routes": [], "count": 0, "default_gw": None})
        data = json.loads(out)
        routes = []
        default_gw = None
        for item in data:
            dst = item.get("dst", "")
            gateway = item.get("gateway") or None
            dev = item.get("dev", "")
            protocol = item.get("protocol", "")
            scope = item.get("scope", "")
            metric = item.get("metric", 0)
            try:
                metric = int(metric)
            except (TypeError, ValueError):
                metric = 0
            prefsrc = item.get("prefsrc") or None
            routes.append({
                "dst": dst,
                "gateway": gateway,
                "dev": dev,
                "protocol": protocol,
                "scope": scope,
                "metric": metric,
                "prefsrc": prefsrc,
            })
            if dst == "default" and gateway:
                default_gw = gateway
        return jsonify({"routes": routes, "count": len(routes), "default_gw": default_gw, "source": "ip-route"})
    except Exception as exc:
        return jsonify({"error": str(exc), "routes": [], "count": 0, "default_gw": None})


# ── Journal Errors ────────────────────────────────────────────────────────────
@app.route("/api/system/journal-errors", methods=["GET"])
@require_auth
def api_system_journal_errors():
    """Return recent systemd journal error and warning entries."""
    _LEVEL_MAP = {0: "emerg", 1: "alert", 2: "crit", 3: "err", 4: "warning",
                  5: "notice", 6: "info", 7: "debug"}
    try:
        import json as _json
        import datetime as _dt

        # Primary: journalctl JSON output for error-priority entries
        cmd = ["journalctl", "-p", "err", "-n", "50", "--no-pager", "--output=json"]
        out, rc = _run(cmd, timeout=10)

        entries = []
        if rc == 0 and out:
            for raw_line in out.splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = _json.loads(raw_line)
                    ts_us = int(obj.get("__REALTIME_TIMESTAMP", 0))
                    if ts_us:
                        ts_iso = _dt.datetime.utcfromtimestamp(ts_us / 1_000_000).strftime("%Y-%m-%dT%H:%M:%SZ")
                    else:
                        ts_iso = ""
                    priority = int(obj.get("PRIORITY", 3))
                    unit = (obj.get("_SYSTEMD_UNIT") or obj.get("SYSLOG_IDENTIFIER") or obj.get("_COMM") or "")
                    message = obj.get("MESSAGE", "")
                    if isinstance(message, list):
                        message = " ".join(str(m) for m in message)
                    entries.append({
                        "ts": ts_iso,
                        "priority": priority,
                        "level": _LEVEL_MAP.get(priority, "err"),
                        "unit": str(unit),
                        "message": str(message),
                    })
                except (ValueError, KeyError):
                    continue

        # Fallback plaintext: warnings + errors for context
        plain_cmd = ["journalctl", "-p", "warning", "-n", "100", "--no-pager", "--output=short-iso"]
        plain_out, plain_rc = _run(plain_cmd, timeout=10)
        plaintext = ""
        if plain_rc == 0 and plain_out:
            lines = [l for l in plain_out.splitlines() if l.strip() and not l.startswith("--")]
            plaintext = "\n".join(lines[-30:])

        error_count = sum(1 for e in entries if e["priority"] <= 3)
        warning_count = sum(1 for e in entries if e["priority"] == 4)
        return jsonify({
            "entries": entries,
            "count": len(entries),
            "has_errors": any(e["priority"] <= 3 for e in entries),
            "error_count": error_count,
            "warning_count": warning_count,
            "plaintext": plaintext,
        })
    except FileNotFoundError:
        return jsonify({"entries": [], "count": 0, "has_errors": False,
                        "error_count": 0, "warning_count": 0, "plaintext": "",
                        "error": "journalctl not available"})
    except Exception as exc:
        return jsonify({"entries": [], "count": 0, "has_errors": False,
                        "error_count": 0, "warning_count": 0, "plaintext": "",
                        "error": str(exc)})


# ── Swap / zRAM Info ──────────────────────────────────────────────────────────

@app.route("/api/system/swap", methods=["GET"])
@require_auth
def api_system_swap():
    """Swap usage and virtual memory stats."""
    result = {"swaps": [], "vmstat": {}, "zram": []}
    try:
        # /proc/swaps
        swaps_out, _ = _run("cat /proc/swaps")
        lines = swaps_out.strip().splitlines()
        if len(lines) > 1:  # skip header
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 5:
                    total_kb = int(parts[2])
                    used_kb = int(parts[3])
                    result["swaps"].append({
                        "filename": parts[0],
                        "type": parts[1],
                        "total_kb": total_kb,
                        "used_kb": used_kb,
                        "free_kb": total_kb - used_kb,
                        "pct_used": round(used_kb / total_kb * 100, 1) if total_kb > 0 else 0,
                        "priority": int(parts[4]),
                    })
    except Exception as e:
        result["swaps_error"] = str(e)

    try:
        # Key vmstat fields
        vmstat_out, _ = _run("cat /proc/vmstat")
        keys_wanted = {
            "pgfault": "page_faults",
            "pgmajfault": "major_faults",
            "pswpin": "swap_in_pages",
            "pswpout": "swap_out_pages",
            "pgpgin": "pages_read_in",
            "pgpgout": "pages_written_out",
            "oom_kill": "oom_kills",
        }
        for line in vmstat_out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in keys_wanted:
                result["vmstat"][keys_wanted[parts[0]]] = int(parts[1])
    except Exception as e:
        result["vmstat_error"] = str(e)

    try:
        # zram devices
        zram_out, _ = _run("ls /sys/block/ 2>/dev/null")
        for dev in zram_out.split():
            if not dev.startswith("zram"):
                continue
            zr = {"device": dev}
            for attr in ("orig_data_size", "compr_data_size", "mem_used_total", "disksize"):
                val, rc = _run(f"cat /sys/block/{dev}/mm_stat 2>/dev/null || cat /sys/block/{dev}/{attr} 2>/dev/null")
                if attr == "orig_data_size" and val.strip():
                    # Try mm_stat: orig compr mem_used
                    parts = val.strip().split()
                    if len(parts) >= 3:
                        zr["orig_bytes"] = int(parts[0])
                        zr["compr_bytes"] = int(parts[1])
                        zr["mem_used_bytes"] = int(parts[2])
                        break
            else:
                ds, _ = _run(f"cat /sys/block/{dev}/disksize 2>/dev/null")
                if ds.strip().isdigit():
                    zr["disksize_bytes"] = int(ds.strip())
            result["zram"].append(zr)
    except Exception:
        pass

    return jsonify(result)


# ── WiFi Network Scan ─────────────────────────────────────────────────────────
@app.route("/api/network/wifi-scan", methods=["GET"])
@require_auth
def api_network_wifi_scan():
    """Return nearby WiFi networks via iw or iwlist scan."""
    import re

    iface = "wlan0"
    networks = []

    def parse_iw(output):
        results = []
        current = {}
        for line in output.splitlines():
            line = line.strip()
            m = re.match(r"^BSS ([0-9a-f:]{17})", line, re.IGNORECASE)
            if m:
                if current:
                    results.append(current)
                current = {"bssid": m.group(1).lower(), "ssid": "", "channel": None,
                           "frequency_mhz": None, "signal_dbm": None,
                           "quality": None, "encryption": "Open"}
                continue
            if not current:
                continue
            m = re.match(r"SSID: (.+)", line)
            if m:
                current["ssid"] = m.group(1).strip()
                continue
            m = re.match(r"freq: (\d+)", line)
            if m:
                freq = int(m.group(1))
                current["frequency_mhz"] = freq
                # Derive channel from frequency
                if 2412 <= freq <= 2484:
                    current["channel"] = (freq - 2407) // 5 if freq != 2484 else 14
                elif 5180 <= freq <= 5825:
                    current["channel"] = (freq - 5000) // 5
                continue
            m = re.match(r"signal: ([-\d.]+) dBm", line)
            if m:
                try:
                    current["signal_dbm"] = int(float(m.group(1)))
                except ValueError:
                    pass
                continue
            if re.search(r"capability:.*Privacy", line, re.IGNORECASE):
                current["encryption"] = "Encrypted"
                continue
            if re.match(r"RSN:", line) or re.match(r"\* Version:", line):
                if current.get("encryption") != "WPA2":
                    current["encryption"] = "WPA2"
                continue
            if re.match(r"WPA:", line):
                if current.get("encryption") not in ("WPA2",):
                    current["encryption"] = "WPA"
                continue
        if current:
            results.append(current)
        return results

    def parse_iwlist(output):
        results = []
        current = {}
        for line in output.splitlines():
            line = line.strip()
            m = re.match(r"Cell \d+ - Address: ([0-9A-Fa-f:]{17})", line)
            if m:
                if current:
                    results.append(current)
                current = {"bssid": m.group(1).lower(), "ssid": "", "channel": None,
                           "frequency_mhz": None, "signal_dbm": None,
                           "quality": None, "encryption": "Open"}
                continue
            if not current:
                continue
            m = re.match(r'ESSID:"(.*)"', line)
            if m:
                current["ssid"] = m.group(1)
                continue
            m = re.match(r"Channel:(\d+)", line)
            if m:
                current["channel"] = int(m.group(1))
                continue
            m = re.match(r"Frequency:([\d.]+) GHz", line)
            if m:
                try:
                    current["frequency_mhz"] = int(float(m.group(1)) * 1000)
                except ValueError:
                    pass
                continue
            m = re.search(r"Signal level=([-\d]+)\s*dBm", line)
            if m:
                try:
                    current["signal_dbm"] = int(m.group(1))
                except ValueError:
                    pass
                continue
            m = re.search(r"Quality=([\d]+)/([\d]+)", line)
            if m:
                current["quality"] = f"{m.group(1)}/{m.group(2)}"
                if current["signal_dbm"] is None:
                    # Some iwlist outputs quality only without dBm
                    try:
                        q = int(m.group(1))
                        t = int(m.group(2))
                        current["signal_dbm"] = -100 + int((q / t) * 50)
                    except (ValueError, ZeroDivisionError):
                        pass
                continue
            if re.match(r"Encryption key:on", line, re.IGNORECASE):
                current["encryption"] = "Encrypted"
                continue
            if re.search(r"IE:.*WPA2", line, re.IGNORECASE):
                current["encryption"] = "WPA2"
                continue
            if re.search(r"IE:.*WPA ", line, re.IGNORECASE):
                if current.get("encryption") != "WPA2":
                    current["encryption"] = "WPA"
                continue
        if current:
            results.append(current)
        return results

    # Try iw first
    out, rc = _run(["iw", "dev", iface, "scan"], timeout=15)
    if rc == 0 and out.strip():
        networks = parse_iw(out)
    else:
        # Fallback to iwlist
        out2, rc2 = _run(["iwlist", iface, "scan"], timeout=15)
        if rc2 == 0 and out2.strip():
            networks = parse_iwlist(out2)
        else:
            return jsonify({"networks": [], "count": 0, "iface": iface,
                            "error": "scan unavailable"})

    # Add quality string for iw results that don't have it yet
    for net in networks:
        if net.get("quality") is None and net.get("signal_dbm") is not None:
            sig = net["signal_dbm"]
            # Map -100..0 dBm to 0..100
            q = max(0, min(100, 2 * (sig + 100)))
            net["quality"] = f"{q}/100"

    # Sort by signal descending (strongest first), treat None as -999
    networks.sort(key=lambda n: n.get("signal_dbm") or -999, reverse=True)
    networks = networks[:20]

    return jsonify({"networks": networks, "count": len(networks), "iface": iface})


# ── Network Interface Counters (/proc/net/dev) ────────────────────────────────
@app.route("/api/network/netdev", methods=["GET"])
@require_auth
def api_network_netdev():
    proc_path = "/proc/net/dev"
    try:
        with open(proc_path, "r") as fh:
            lines = fh.readlines()
    except OSError as exc:
        return jsonify({"interfaces": [], "count": 0, "error": str(exc)})

    interfaces = []
    # Skip the 2-line header
    for line in lines[2:]:
        line = line.strip()
        if not line:
            continue
        iface, _, stats = line.partition(":")
        iface = iface.strip()
        if iface == "lo":
            continue
        fields = stats.split()
        if len(fields) < 16:
            continue
        try:
            interfaces.append({
                "iface": iface,
                "rx_bytes":   int(fields[0]),
                "rx_packets": int(fields[1]),
                "rx_errors":  int(fields[2]),
                "rx_dropped": int(fields[3]),
                "tx_bytes":   int(fields[8]),
                "tx_packets": int(fields[9]),
                "tx_errors":  int(fields[10]),
                "tx_dropped": int(fields[11]),
            })
        except (ValueError, IndexError):
            continue

    interfaces.sort(key=lambda x: x["iface"])
    return jsonify({"interfaces": interfaces, "count": len(interfaces)})


# ── TCP Connections ───────────────────────────────────────────────────────────

_TCP_STATES = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "03": "SYN_RECV",
    "04": "FIN_WAIT1",
    "05": "FIN_WAIT2",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "09": "LAST_ACK",
    "0A": "LISTEN",
    "0B": "CLOSING",
}


def _parse_tcp_addr_v4(hex_addr):
    """Convert hex 'AABBCCDD:PPPP' to ('ddd.ddd.ddd.ddd', port_int)."""
    addr_hex, port_hex = hex_addr.split(":")
    # Linux stores IPv4 in little-endian hex
    ip_int = int(addr_hex, 16)
    ip = (
        f"{ip_int & 0xFF}.{(ip_int >> 8) & 0xFF}"
        f".{(ip_int >> 16) & 0xFF}.{(ip_int >> 24) & 0xFF}"
    )
    port = int(port_hex, 16)
    return ip, port


def _parse_tcp_addr_v6(hex_addr):
    """Convert hex IPv6 addr:port to ('x:x:x:x:x:x:x:x', port_int)."""
    addr_hex, port_hex = hex_addr.split(":")
    # Four 32-bit little-endian words
    groups = []
    for i in range(0, 32, 8):
        word = int(addr_hex[i:i + 8], 16)
        hi = (word & 0xFFFF)
        lo = (word >> 16) & 0xFFFF
        groups.append(f"{hi:04x}")
        groups.append(f"{lo:04x}")
    ip = ":".join(groups)
    port = int(port_hex, 16)
    return ip, port


def _is_loopback_v4(ip):
    return ip.startswith("127.")


def _is_loopback_v6(ip):
    # ::1 expanded
    return ip in ("00000000:00000000:00000000:00000000:00000000:00000000:00000000:00000001",
                  "0000:0000:0000:0000:0000:0000:0000:0001") or ip == "::1"


def _parse_proc_tcp(path, family):
    """Parse /proc/net/tcp or /proc/net/tcp6. Returns list of connection dicts."""
    conns = []
    try:
        with open(path, "r") as fh:
            lines = fh.readlines()
    except OSError:
        return conns

    for line in lines[1:]:  # skip header
        parts = line.split()
        if len(parts) < 10:
            continue
        local_raw = parts[1]
        remote_raw = parts[2]
        state_hex = parts[3].upper()
        state = _TCP_STATES.get(state_hex, state_hex)

        try:
            if family == "ipv4":
                local_ip, local_port = _parse_tcp_addr_v4(local_raw)
                remote_ip, remote_port = _parse_tcp_addr_v4(remote_raw)
                if _is_loopback_v4(local_ip) and _is_loopback_v4(remote_ip):
                    continue
            else:
                local_ip, local_port = _parse_tcp_addr_v6(local_raw)
                remote_ip, remote_port = _parse_tcp_addr_v6(remote_raw)
                if _is_loopback_v6(local_ip) and _is_loopback_v6(remote_ip):
                    continue
        except (ValueError, IndexError):
            continue

        conns.append({
            "local_ip": local_ip,
            "local_port": local_port,
            "remote_ip": remote_ip,
            "remote_port": remote_port,
            "state": state,
            "family": family,
        })

    return conns


def _tcp_sort_key(conn):
    order = {"LISTEN": 0, "ESTABLISHED": 1}
    return order.get(conn["state"], 2)


@app.route("/api/network/tcp", methods=["GET"])
@require_auth
def api_network_tcp():
    conns = []
    error = None

    for path, family in [("/proc/net/tcp", "ipv4"), ("/proc/net/tcp6", "ipv6")]:
        try:
            conns.extend(_parse_proc_tcp(path, family))
        except Exception as exc:  # pylint: disable=broad-except
            error = str(exc)

    if not conns and error:
        return jsonify({"connections": [], "count": 0, "error": error})

    conns.sort(key=_tcp_sort_key)
    conns = conns[:50]

    established = sum(1 for c in conns if c["state"] == "ESTABLISHED")
    listening = sum(1 for c in conns if c["state"] == "LISTEN")

    return jsonify({
        "connections": conns,
        "count": len(conns),
        "listening": listening,
        "established": established,
    })


# ── Battery / UPS Status ──────────────────────────────────────────────────────

@app.route("/api/system/battery", methods=["GET"])
@require_auth
def api_system_battery():
    """Return battery/UPS status from /sys/class/power_supply/ sysfs entries."""
    ps_dir = Path("/sys/class/power_supply")
    _empty = {"supplies": [], "count": 0, "has_battery": False, "source": "sysfs"}
    try:
        entry_names = sorted(p.name for p in ps_dir.iterdir())
    except OSError:
        return jsonify(_empty)

    if not entry_names:
        return jsonify(_empty)

    def _read(base, fname):
        try:
            return (base / fname).read_text().strip()
        except OSError:
            return None

    # First pass: collect all supplies with their types
    raw = []
    for name in entry_names:
        base = ps_dir / name
        ptype = _read(base, "type") or "Unknown"
        raw.append((name, base, ptype))

    # Filter Mains/USB unless they are the only supplies present
    non_mains = [(n, b, t) for n, b, t in raw if t not in ("Mains", "USB")]
    to_process = non_mains if non_mains else raw

    supplies = []
    for name, base, ptype in to_process:
        def _int_field(base, fname, divisor, ndigits):  # noqa: E306
            val = _read(base, fname)
            if val is None:
                return None
            try:
                return round(int(val) / divisor, ndigits)
            except ValueError:
                return None

        status = _read(base, "status") or "Unknown"

        capacity_raw = _read(base, "capacity")
        try:
            capacity_pct = int(capacity_raw) if capacity_raw is not None else None
        except ValueError:
            capacity_pct = None

        voltage_v      = _int_field(base, "voltage_now",  1_000_000, 3)
        current_ma     = _int_field(base, "current_now",  1_000,     1)
        power_mw       = _int_field(base, "power_now",    1_000,     1)
        energy_wh      = _int_field(base, "energy_now",   1_000_000, 3)
        energy_full_wh = _int_field(base, "energy_full",  1_000_000, 3)

        supplies.append({
            "name":           name,
            "type":           ptype,
            "status":         status,
            "capacity_pct":   capacity_pct,
            "voltage_v":      voltage_v,
            "current_ma":     current_ma,
            "power_mw":       power_mw,
            "energy_wh":      energy_wh,
            "energy_full_wh": energy_full_wh,
            "manufacturer":   _read(base, "manufacturer"),
            "model":          _read(base, "model_name"),
            "technology":     _read(base, "technology"),
        })

    has_battery = any(s["type"] == "Battery" for s in supplies)
    return jsonify({
        "supplies":    supplies,
        "count":       len(supplies),
        "has_battery": has_battery,
        "source":      "sysfs",
    })


# ── IRQ Interrupts ────────────────────────────────────────────────────────────
@app.route("/api/system/interrupts", methods=["GET"])
@require_auth
def api_system_interrupts():
    """Return top IRQ interrupt counts from /proc/interrupts."""
    try:
        lines = Path("/proc/interrupts").read_text().splitlines()
    except OSError as exc:
        return jsonify({"cpu_count": 0, "total": 0, "irqs": [], "error": str(exc)})

    if not lines:
        return jsonify({"cpu_count": 0, "total": 0, "irqs": [], "error": "empty file"})

    # First line: CPU headers — count CPUs
    cpu_count = len(lines[0].split())

    results = []
    for line in lines[1:]:
        parts = line.split()
        if not parts:
            continue
        irq = parts[0].rstrip(":")
        # Count fields are next cpu_count integers
        try:
            counts = [int(parts[i + 1]) for i in range(cpu_count)]
        except (IndexError, ValueError):
            continue
        total = sum(counts)
        if total == 0:
            continue
        # Remaining fields after the counts: optional type + description
        remainder = parts[1 + cpu_count:]
        irq_type = remainder[0] if len(remainder) > 0 else None
        description = " ".join(remainder[1:]) if len(remainder) > 1 else ""
        results.append({
            "irq": irq,
            "total": total,
            "type": irq_type,
            "description": description,
        })

    results.sort(key=lambda x: x["total"], reverse=True)
    top = results[:20]
    grand_total = sum(r["total"] for r in results)
    return jsonify({"cpu_count": cpu_count, "total": grand_total, "irqs": top})


# ── Top Processes by Memory ───────────────────────────────────────────────────
@app.route("/api/system/proc-mem", methods=["GET"])
@require_auth
def api_system_proc_mem():
    """Return top processes sorted by RSS from /proc/<pid>/status."""
    try:
        raw_limit = request.args.get("limit", "20")
        limit = int(raw_limit)
    except (ValueError, TypeError):
        return jsonify({"error": "limit must be an integer"}), 400
    limit = max(1, min(limit, 50))

    processes = []
    try:
        pids = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError as exc:
        return jsonify({"processes": [], "count": 0, "total_rss_kb": 0, "error": str(exc)})

    for pid in pids:
        try:
            with open(f"/proc/{pid}/status", "r") as fh:
                raw = fh.read()
        except OSError:
            # Process exited between listing and reading — normal race condition
            continue

        info = {}
        for line in raw.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            info[key.strip()] = val.strip()

        try:
            rss_kb = int(info.get("VmRSS", "0 kB").split()[0])
            vsz_kb = int(info.get("VmSize", "0 kB").split()[0])
        except (ValueError, IndexError):
            rss_kb = 0
            vsz_kb = 0

        state_raw = info.get("State", "")
        state = state_raw.split()[0] if state_raw else ""

        processes.append({
            "pid": int(pid),
            "name": info.get("Name", ""),
            "rss_kb": rss_kb,
            "vsz_kb": vsz_kb,
            "state": state,
        })

    processes.sort(key=lambda x: x["rss_kb"], reverse=True)
    top = processes[:limit]
    total_rss_kb = sum(p["rss_kb"] for p in processes)
    return jsonify({"processes": top, "count": len(top), "total_rss_kb": total_rss_kb})


# ── Disk Partitions ───────────────────────────────────────────────────────────


@app.route("/api/system/disk-partitions", methods=["GET"])
@require_auth
def api_system_disk_partitions():
    """Return disk partition usage parsed from df -P -k, merged with lsblk device info."""
    skip_fs = {"tmpfs", "devtmpfs", "udev", "none", "overlay", "squashfs"}

    # Build lsblk model map keyed by mountpoint
    model_by_mount = {}
    lsblk_out, lsblk_rc = _run(["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,MODEL"])
    if lsblk_rc == 0 and lsblk_out.strip():
        try:
            import json as _json
            lsblk_data = _json.loads(lsblk_out)
            def _walk(devices):
                for dev in devices:
                    mp = dev.get("mountpoint") or ""
                    model = (dev.get("model") or "").strip()
                    if mp and model:
                        model_by_mount[mp] = model
                    children = dev.get("children") or []
                    _walk(children)
            _walk(lsblk_data.get("blockdevices") or [])
        except Exception:
            pass

    partitions = []
    df_out, df_rc = _run(["df", "-P", "-k"])
    if df_rc != 0:
        return jsonify({"error": "df failed", "partitions": [], "count": 0}), 500

    for line in df_out.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 6:
            continue
        device = cols[0]
        size_kb_str = cols[1]
        used_kb_str = cols[2]
        free_kb_str = cols[3]
        mountpoint = cols[5]

        # df -P doesn't output fstype; skip virtual/pseudo devices by name
        # We check filesystem type from /proc/mounts as a fallback
        fstype = ""
        try:
            with open("/proc/mounts") as _f:
                for _line in _f:
                    _parts = _line.split()
                    if len(_parts) >= 3 and _parts[1] == mountpoint and _parts[0] == device:
                        fstype = _parts[2]
                        break
        except OSError:
            pass

        if fstype in skip_fs:
            continue

        try:
            size_kb = int(size_kb_str)
            used_kb = int(used_kb_str)
            free_kb = int(free_kb_str)
            pct_used = round(used_kb / size_kb * 100, 1) if size_kb > 0 else 0.0
        except ValueError:
            continue

        entry = {
            "device": device,
            "mountpoint": mountpoint,
            "fstype": fstype,
            "size_kb": size_kb,
            "used_kb": used_kb,
            "free_kb": free_kb,
            "pct_used": pct_used,
        }
        model = model_by_mount.get(mountpoint, "")
        if model:
            entry["model"] = model

        partitions.append(entry)

    return jsonify({"partitions": partitions, "count": len(partitions)})


# ── Kernel Log (dmesg) ────────────────────────────────────────────────────────

@app.route("/api/system/dmesg")
@require_auth
def api_dmesg():
    try:
        lines_param = request.args.get("lines", 30)
        try:
            lines_count = int(lines_param)
        except (ValueError, TypeError):
            lines_count = 30
        lines_count = max(1, min(lines_count, 100))

        out, rc = _run(
            ["dmesg", "--time-format", "iso", "-l", "warn,err,crit,alert,emerg",
             "-n", str(lines_count)],
            timeout=10,
        )
        if rc != 0:
            out, rc = _run(
                ["dmesg", "-T"],
                timeout=10,
            )
            if rc != 0:
                return jsonify({"messages": [], "count": 0, "error": "dmesg unavailable"})
            raw_lines = out.splitlines()[-lines_count:]
        else:
            raw_lines = [l for l in out.splitlines() if l.strip()]

        messages = []
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            ts = ""
            msg = line
            # ISO timestamp: starts with digit or [
            m = re.match(r'^(\[?\d{4}-\d{2}-\d{2}T[\d:.+-]+\]?)\s+(.*)', line)
            if m:
                ts = m.group(1).strip("[]")
                msg = m.group(2)
            else:
                # bracketed seconds-since-boot: [   12.345678]
                m2 = re.match(r'^\[\s*[\d.]+\]\s+(.*)', line)
                if m2:
                    msg = m2.group(1)

            lower = msg.lower()
            if "error" in lower or "err:" in lower:
                level = "err"
            elif "warn" in lower:
                level = "warn"
            else:
                level = "info"

            messages.append({"ts": ts, "level": level, "msg": msg})

        return jsonify({"messages": messages, "count": len(messages), "error": None})
    except Exception as exc:  # pylint: disable=broad-except
        return jsonify({"messages": [], "count": 0, "error": str(exc)})


@app.route("/api/network/sockstat")
@require_auth
def api_network_sockstat():
    """Parse /proc/net/sockstat and /proc/net/sockstat6, return socket counts."""
    def parse_sockstat(path):
        result = {}
        try:
            text = Path(path).read_text()
        except OSError:
            return result
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            proto = parts[0].rstrip(":").lower()
            fields = {}
            i = 1
            while i + 1 < len(parts):
                try:
                    fields[parts[i]] = int(parts[i + 1])
                except (ValueError, IndexError):
                    pass
                i += 2
            if fields:
                result[proto] = fields
        return result

    data = parse_sockstat("/proc/net/sockstat")
    data6 = parse_sockstat("/proc/net/sockstat6")

    def pick(src, key, *fields):
        entry = src.get(key, {})
        return {f: entry.get(f, 0) for f in fields}

    return jsonify({
        "sockets": pick(data, "sockets", "used"),
        "tcp": pick(data, "tcp", "inuse", "orphan", "tw", "alloc"),
        "udp": pick(data, "udp", "inuse"),
        "raw": pick(data, "raw", "inuse"),
        "frag": pick(data, "frag", "inuse", "memory"),
        "tcp6": pick(data6, "tcp6", "inuse"),
        "udp6": pick(data6, "udp6", "inuse"),
    })


@app.route("/api/system/osinfo")
@require_auth
def api_system_osinfo():
    """Return OS/kernel/arch/uptime/hostname info."""
    # Kernel version
    try:
        kernel = Path("/proc/version").read_text().strip()
    except OSError:
        kernel = "unknown"

    # Distro
    distro = "unknown"
    try:
        osrelease = Path("/etc/os-release").read_text()
        for line in osrelease.splitlines():
            if line.startswith("PRETTY_NAME="):
                distro = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        try:
            distro = Path("/etc/debian_version").read_text().strip()
        except OSError:
            pass

    # Architecture
    arch_out, rc = _run(["uname", "-m"])
    arch = arch_out.strip() if rc == 0 else "unknown"

    # Uptime
    uptime_out, rc = _run(["uptime", "-p"])
    if rc == 0 and uptime_out.strip():
        uptime = uptime_out.strip()
    else:
        try:
            raw = Path("/proc/uptime").read_text().split()[0]
            total_secs = int(float(raw))
            days, remainder = divmod(total_secs, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes = remainder // 60
            parts = []
            if days:
                parts.append(f"{days} day{'s' if days != 1 else ''}")
            if hours:
                parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
            if minutes or not parts:
                parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
            uptime = "up " + ", ".join(parts)
        except (OSError, ValueError, IndexError):
            uptime = "unknown"

    # Hostname
    try:
        hostname = Path("/proc/sys/kernel/hostname").read_text().strip()
    except OSError:
        hostname = "unknown"

    return jsonify({
        "kernel": kernel,
        "distro": distro,
        "arch": arch,
        "uptime": uptime,
        "hostname": hostname,
    })


@app.route("/api/system/loadavg", methods=["GET"])
@require_auth
def api_system_loadavg():
    """Return load average and process counts from /proc/loadavg and CPU count from /proc/cpuinfo."""
    try:
        with open("/proc/loadavg", "r") as fh:
            raw = fh.read().strip()
        parts = raw.split()
        load_1 = float(parts[0])
        load_5 = float(parts[1])
        load_15 = float(parts[2])
        procs = parts[3].split("/")
        running_procs = int(procs[0])
        total_procs = int(procs[1])

        cpu_count = 0
        with open("/proc/cpuinfo", "r") as fh:
            for line in fh:
                if line.startswith("processor"):
                    cpu_count += 1
        if cpu_count == 0:
            cpu_count = 1

        normalized_1 = round(load_1 / cpu_count, 3)

        return jsonify({
            "load_1": load_1,
            "load_5": load_5,
            "load_15": load_15,
            "cpu_count": cpu_count,
            "running_procs": running_procs,
            "total_procs": total_procs,
            "normalized_1": normalized_1,
        })
    except Exception as exc:  # pylint: disable=broad-except
        return jsonify({"error": str(exc)}), 500


@app.route("/api/network/firewall-stats")
@require_auth
def api_network_firewall_stats():
    """Return iptables chain stats (policy, packets, bytes, rule count) for IPv4 and IPv6."""

    def _parse_bytes(val):
        """Convert iptables byte value (e.g. '567K', '2M') to integer bytes."""
        val = val.strip()
        if not val or val == "0":
            return 0
        suffixes = {"K": 1024, "M": 1048576, "G": 1073741824}
        for suffix, mult in suffixes.items():
            if val.endswith(suffix):
                try:
                    return int(float(val[:-1]) * mult)
                except ValueError:
                    return 0
        try:
            return int(val)
        except ValueError:
            return 0

    def _parse_iptables(output):
        """Parse iptables -L -n -v --line-numbers output into chain dicts."""
        chains = {}
        current_chain = None
        rule_count = 0
        for line in output.splitlines():
            # Chain header: Chain INPUT (policy ACCEPT 1234 packets, 567K bytes)
            m = re.match(
                r"^Chain\s+(\S+)\s+\(policy\s+(\S+)\s+(\S+)\s+packets,\s+(\S+)\s+bytes\)",
                line,
            )
            if m:
                if current_chain is not None:
                    chains[current_chain]["rules"] = rule_count
                current_chain = m.group(1)
                rule_count = 0
                chains[current_chain] = {
                    "policy": m.group(2),
                    "packets": _parse_bytes(m.group(3)),
                    "bytes": _parse_bytes(m.group(4)),
                    "rules": 0,
                }
                continue
            # Skip header/blank lines; count rule lines (start with a digit)
            if current_chain is not None and re.match(r"^\d+\s+", line):
                rule_count += 1
        if current_chain is not None:
            chains[current_chain]["rules"] = rule_count
        return chains

    result = {"ipv4": {}, "ipv6": {}, "error": None}

    out4, rc4 = _run("iptables -L -n -v --line-numbers 2>&1", timeout=10)
    if rc4 != 0 or "iptables" not in out4.lower() and not out4.strip():
        result["error"] = "iptables not available"
        return jsonify(result)
    if "permission denied" in out4.lower() or "operation not permitted" in out4.lower():
        result["error"] = "iptables not available"
        return jsonify(result)
    result["ipv4"] = _parse_iptables(out4)

    out6, rc6 = _run("ip6tables -L -n -v --line-numbers 2>&1", timeout=10)
    if rc6 == 0:
        result["ipv6"] = _parse_iptables(out6)

    return jsonify(result)


# ── USB Devices ──────────────────────────────────────────────────────────────

@app.route("/api/system/usb-devices")
@require_auth
def api_system_usb_devices():
    devices = []
    out, rc = _run("lsusb")
    if rc != 0:
        err = out.strip() if out.strip() else "lsusb failed or not found"
        return jsonify({"error": err, "devices": [], "count": 0})
    for line in out.splitlines():
        # Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
        m = re.match(r'Bus (\d+) Device (\d+): ID ([0-9a-f:]+)\s+(.*)', line)
        if m:
            devices.append({"bus": m.group(1), "device": m.group(2),
                             "id": m.group(3), "description": m.group(4).strip()})
    return jsonify({"devices": devices, "count": len(devices), "source": "lsusb"})


@app.route("/api/system/cpu-temp", methods=["GET"])
@require_auth
def api_system_cpu_temp():
    """Return CPU thermal zone temperatures from /sys/class/thermal."""
    try:
        zones = []
        thermal_base = Path("/sys/class/thermal")
        for zone_path in sorted(thermal_base.glob("thermal_zone*")):
            try:
                zone_type = (zone_path / "type").read_text().strip()
                temp_raw = (zone_path / "temp").read_text().strip()
                temp_c = round(int(temp_raw) / 1000.0, 1)
                if "acpi" in zone_type.lower() and temp_c == 0.0:
                    continue
                zones.append({"zone": zone_type, "temp_c": temp_c})
            except (OSError, ValueError):
                continue
        max_temp = max((z["temp_c"] for z in zones), default=None)
        return jsonify({"zones": zones, "max_temp_c": max_temp})
    except Exception as exc:  # pylint: disable=broad-except
        return jsonify({"zones": [], "max_temp_c": None, "error": str(exc)}), 500


# ── Kernel Modules (v2) ───────────────────────────────────────────────────────

@app.route("/api/system/kernel-modules", methods=["GET"])
@require_auth
def api_system_kernel_modules():
    try:
        kernel_ver = None
        uname_out, uname_rc = _run("uname -r 2>/dev/null")
        if uname_rc == 0:
            kernel_ver = uname_out.strip()
        out, rc = _run("lsmod 2>/dev/null")
        if rc != 0:
            return jsonify({"modules": [], "count": 0, "kernel": kernel_ver, "error": "lsmod failed"})
        modules = []
        lines = out.strip().splitlines()
        for line in lines[1:]:  # skip header
            cols = line.split()
            if len(cols) < 3:
                continue
            name = cols[0]
            try:
                size = int(cols[1])
            except ValueError:
                size = 0
            try:
                used_count = int(cols[2])
            except ValueError:
                used_count = 0
            raw_deps = cols[3].strip(",") if len(cols) > 3 else ""
            used_by = [d for d in raw_deps.split(",") if d] if raw_deps else []
            modules.append({
                "name": name,
                "size": size,
                "used_count": used_count,
                "used_by": used_by,
            })
        modules.sort(key=lambda m: m["size"], reverse=True)
        return jsonify({"modules": modules, "count": len(modules), "kernel": kernel_ver, "error": None})
    except Exception as exc:  # pylint: disable=broad-except
        return jsonify({"modules": [], "count": 0, "kernel": None, "error": str(exc)})


@app.route("/api/system/logged-in-users", methods=["GET"])
@require_auth
def api_system_logged_in_users():
    """Return currently logged-in users parsed from 'who -u'."""
    try:
        out, rc = _run("who -u")
        if rc != 0 or not out.strip():
            return jsonify({"users": [], "count": 0})
        users = []
        for line in out.splitlines():
            parts = line.split()
            # who -u format: user tty date time [idle] pid [from]
            # minimum fields: user tty date time idle pid
            if len(parts) < 6:
                continue
            username = parts[0]
            tty = parts[1]
            login_time = parts[2] + " " + parts[3]
            idle = parts[4]
            try:
                pid = int(parts[5])
            except ValueError:
                pid = 0
            from_host = parts[6] if len(parts) >= 7 else ""
            # Strip surrounding parens from from field if present
            if from_host.startswith("(") and from_host.endswith(")"):
                from_host = from_host[1:-1]
            users.append({
                "username": username,
                "tty": tty,
                "login_time": login_time,
                "idle": idle,
                "pid": pid,
                "from": from_host,
            })
        return jsonify({"users": users, "count": len(users)})
    except Exception as exc:  # pylint: disable=broad-except
        return jsonify({"users": [], "count": 0, "error": str(exc)}), 500


# ── Disk I/O Stats (spec-aligned) ────────────────────────────────────────────

@app.route("/api/system/disk-io", methods=["GET"])
@require_auth
def api_system_disk_io():
    """Parse /proc/diskstats for block devices (exclude loop/ram devices)."""
    import re as _re
    devices = []
    try:
        with open("/proc/diskstats", "r") as fh:
            for line in fh:
                cols = line.split()
                if len(cols) < 14:
                    continue
                name = cols[2]
                if _re.search(r"^(loop|ram)\d+", name):
                    continue
                try:
                    reads_completed  = int(cols[3])
                    reads_merged     = int(cols[4])
                    sectors_read     = int(cols[5])
                    time_reading_ms  = int(cols[6])
                    writes_completed = int(cols[7])
                    writes_merged    = int(cols[8])
                    sectors_written  = int(cols[9])
                    time_writing_ms  = int(cols[10])
                except (ValueError, IndexError):
                    continue
                devices.append({
                    "name":             name,
                    "reads_completed":  reads_completed,
                    "reads_merged":     reads_merged,
                    "read_bytes":       sectors_read * 512,
                    "time_reading_ms":  time_reading_ms,
                    "writes_completed": writes_completed,
                    "writes_merged":    writes_merged,
                    "written_bytes":    sectors_written * 512,
                    "time_writing_ms":  time_writing_ms,
                })
    except Exception as exc:  # pylint: disable=broad-except
        return jsonify({"devices": [], "error": str(exc)})
    return jsonify({"devices": devices})


@app.route("/api/system/cpu-stats", methods=["GET"])
@require_auth
def api_system_cpu_stats():
    """Parse /proc/stat for overall and per-core CPU utilisation percentages."""
    FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq")
    try:
        with open("/proc/stat", "r") as fh:
            lines = fh.readlines()
    except Exception as exc:  # pylint: disable=broad-except
        return jsonify({"total": {}, "cores": [], "num_cores": 0, "error": str(exc)})

    def parse_line(line):
        parts = line.split()
        values = [int(x) for x in parts[1:len(FIELDS) + 1]]
        total = sum(values)
        if total == 0:
            return {f: 0.0 for f in FIELDS}
        return {f: round(values[i] / total * 100, 1) for i, f in enumerate(FIELDS)}

    total_pct = {}
    cores = []
    try:
        for line in lines:
            if line.startswith("cpu ") or line.startswith("cpu\t"):
                total_pct = parse_line(line)
            elif line.startswith("cpu") and len(line) > 3 and line[3].isdigit():
                parts = line.split()
                core_id = int(parts[0][3:])
                cores.append({"core": core_id, **parse_line(line)})
    except Exception as exc:  # pylint: disable=broad-except
        return jsonify({"total": {}, "cores": [], "num_cores": 0, "error": str(exc)})

    return jsonify({"total": total_pct, "cores": cores, "num_cores": len(cores)})


@app.route("/api/system/sysctl", methods=["GET"])
@require_auth
def api_system_sysctl():
    """Read key sysctl values directly from /proc/sys/ paths."""
    _SYSCTL_MAP = [
        ("net.ipv4.ip_forward",          "/proc/sys/net/ipv4/ip_forward"),
        ("net.ipv6.conf.all.forwarding",  "/proc/sys/net/ipv6/conf/all/forwarding"),
        ("net.ipv4.tcp_syncookies",       "/proc/sys/net/ipv4/tcp_syncookies"),
        ("net.ipv4.conf.all.rp_filter",   "/proc/sys/net/ipv4/conf/all/rp_filter"),
        ("net.core.rmem_max",             "/proc/sys/net/core/rmem_max"),
        ("net.core.wmem_max",             "/proc/sys/net/core/wmem_max"),
        ("vm.swappiness",                 "/proc/sys/vm/swappiness"),
        ("vm.dirty_ratio",                "/proc/sys/vm/dirty_ratio"),
        ("kernel.hostname",               "/proc/sys/kernel/hostname"),
        ("kernel.randomize_va_space",     "/proc/sys/kernel/randomize_va_space"),
    ]
    params = []
    try:
        for key, path in _SYSCTL_MAP:
            p = Path(path)
            if not p.exists():
                continue
            try:
                raw = p.read_text().strip()
            except OSError:
                continue
            try:
                value = int(raw)
            except ValueError:
                value = raw
            params.append({"key": key, "value": value, "path": path})
    except Exception as exc:  # pylint: disable=broad-except
        return jsonify({"params": [], "error": str(exc)})
    return jsonify({"params": params})


@app.route("/api/system/memory-breakdown", methods=["GET"])
@require_auth
def api_system_memory_breakdown():
    """Parse /proc/meminfo and return a detailed memory breakdown."""
    fields = {
        "MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached", "SwapCached",
        "Active", "Inactive", "Active(anon)", "Inactive(anon)", "Active(file)",
        "Inactive(file)", "SwapTotal", "SwapFree", "Dirty", "Writeback", "Shmem",
        "Slab", "SReclaimable", "SUnreclaim",
    }
    try:
        data: dict = {}
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                if key not in fields:
                    continue
                val_str = parts[1].strip().split()[0]
                data[key] = int(val_str) * 1024
        result: dict = {}
        for key, val in data.items():
            norm = key.replace("(", "_").replace(")", "").lower()
            result[norm] = val
        mem_total = data.get("MemTotal", 0)
        mem_avail = data.get("MemAvailable", 0)
        used = mem_total - mem_avail
        result["used_bytes"] = used
        result["used_pct"] = round(used / mem_total * 100, 2) if mem_total > 0 else 0.0
        return jsonify(result)
    except Exception as exc:  # pylint: disable=broad-except
        return jsonify({"error": str(exc)})


@app.route("/api/network/ping")
@require_auth
def api_network_ping():
    """Ping multiple hosts and return latency/loss stats."""
    hosts = [
        {"name": "Gateway", "host": "192.168.4.1"},
        {"name": "Cloudflare DNS", "host": "1.1.1.1"},
        {"name": "Google DNS", "host": "8.8.8.8"},
        {"name": "Tailscale relay", "host": "100.100.100.100"},
    ]
    results = []
    for entry in hosts:
        out, rc = _run(["ping", "-c", "3", "-W", "2", "-q", entry["host"]])
        record = {"name": entry["name"], "host": entry["host"], "reachable": rc == 0}
        if rc == 0:
            # parse summary line: 3 packets transmitted, 3 received, 0% packet loss
            for line in out.splitlines():
                if "packet loss" in line:
                    parts = line.split(",")
                    for p in parts:
                        p = p.strip()
                        if "transmitted" in p:
                            record["transmitted"] = int(p.split()[0])
                        elif "received" in p:
                            record["received"] = int(p.split()[0])
                        elif "packet loss" in p:
                            record["loss_pct"] = p.split()[0]
                # parse rtt line: rtt min/avg/max/mdev = 1.234/2.345/3.456/0.567 ms
                if "rtt" in line or "round-trip" in line:
                    try:
                        stats = line.split("=")[1].strip().split("/")
                        record["rtt_min"] = stats[0].strip()
                        record["rtt_avg"] = stats[1].strip()
                        record["rtt_max"] = stats[2].strip()
                    except (IndexError, ValueError):
                        pass
        results.append(record)
    return jsonify({"hosts": results})


@app.route("/api/system/cron-jobs")
@require_auth
def api_system_cron_jobs():
    """Return scheduled cron jobs from system crontabs and user crontab."""
    import glob
    jobs = []

    def _parse_crontab_lines(lines, source):
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("@"):
                parts = line.split(None, 1)
                jobs.append({"schedule": parts[0], "command": parts[1] if len(parts) > 1 else "", "source": source})
            else:
                parts = line.split(None, 5)
                if len(parts) >= 6:
                    schedule = " ".join(parts[:5])
                    jobs.append({"schedule": schedule, "command": parts[5], "source": source})
                elif len(parts) >= 5:
                    schedule = " ".join(parts[:5])
                    jobs.append({"schedule": schedule, "command": "", "source": source})

    # /etc/crontab
    try:
        content = Path("/etc/crontab").read_text()
        _parse_crontab_lines(content.splitlines(), "/etc/crontab")
    except OSError:
        pass

    # /etc/cron.d/*
    for path in sorted(glob.glob("/etc/cron.d/*")):
        try:
            content = Path(path).read_text()
            _parse_crontab_lines(content.splitlines(), path)
        except OSError:
            pass

    # root crontab
    out, rc = _run(["crontab", "-l", "-u", "root"])
    if rc == 0:
        _parse_crontab_lines(out.splitlines(), "crontab(root)")

    return jsonify({"jobs": jobs, "count": len(jobs)})


@app.route("/api/system/mounts")
@require_auth
def api_system_mounts():
    """Return mounted filesystems with type, options, and inode/space usage."""
    mounts = []
    skip_types = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup",
                  "cgroup2", "pstore", "debugfs", "tracefs", "securityfs",
                  "fusectl", "hugetlbfs", "mqueue", "ramfs", "bpf", "configfs"}
    try:
        content = Path("/proc/mounts").read_text()
    except OSError:
        return jsonify({"mounts": [], "error": "cannot read /proc/mounts"})

    for line in content.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        device, mountpoint, fstype, options = parts[0], parts[1], parts[2], parts[3]
        if fstype in skip_types:
            continue
        entry = {"device": device, "mountpoint": mountpoint, "fstype": fstype, "options": options.split(",")}
        # disk usage via df
        out, rc = _run(["df", "-B1", "--output=size,used,avail,pcent", mountpoint])
        if rc == 0:
            lines = out.strip().splitlines()
            if len(lines) >= 2:
                vals = lines[1].split()
                if len(vals) >= 4:
                    entry["size_bytes"] = int(vals[0])
                    entry["used_bytes"] = int(vals[1])
                    entry["avail_bytes"] = int(vals[2])
                    entry["use_pct"] = vals[3]
        # inode usage via df -i
        out2, rc2 = _run(["df", "-i", "--output=iused,iavail,ipcent", mountpoint])
        if rc2 == 0:
            lines2 = out2.strip().splitlines()
            if len(lines2) >= 2:
                vals2 = lines2[1].split()
                if len(vals2) >= 3:
                    try:
                        entry["inodes_used"] = int(vals2[0])
                        entry["inodes_avail"] = int(vals2[1])
                        entry["inode_pct"] = vals2[2]
                    except ValueError:
                        pass
        mounts.append(entry)
    return jsonify({"mounts": mounts, "count": len(mounts)})


@app.route("/api/system/top-processes")
@require_auth
def api_system_top_processes():
    """Return top processes by CPU and memory usage."""
    # top 15 by CPU
    out_cpu, rc_cpu = _run([
        "ps", "aux", "--sort=-%cpu",
        "--no-headers", "-o", "pid,user,%cpu,%mem,vsz,rss,comm"
    ])
    # top 15 by memory
    out_mem, rc_mem = _run([
        "ps", "aux", "--sort=-%mem",
        "--no-headers", "-o", "pid,user,%cpu,%mem,vsz,rss,comm"
    ])

    def _parse_ps(output, limit=15):
        procs = []
        for line in output.strip().splitlines()[:limit]:
            parts = line.split(None, 6)
            if len(parts) < 7:
                continue
            procs.append({
                "pid": parts[0],
                "user": parts[1],
                "cpu_pct": parts[2],
                "mem_pct": parts[3],
                "vsz_kb": parts[4],
                "rss_kb": parts[5],
                "comm": parts[6],
            })
        return procs

    return jsonify({
        "by_cpu": _parse_ps(out_cpu) if rc_cpu == 0 else [],
        "by_mem": _parse_ps(out_mem) if rc_mem == 0 else [],
    })
@app.route("/api/network/wifi-info")
@require_auth
def api_network_wifi_info():
    """Return detailed WiFi interface info: SSID, signal, channel, bitrate."""
    result = {}

    # iw dev — list wireless interfaces
    out_dev, rc_dev = _run(["iw", "dev"])
    if rc_dev != 0:
        return jsonify({"error": "iw not available", "interfaces": []})

    interfaces = []
    current_iface = None
    for line in out_dev.splitlines():
        line = line.strip()
        if line.startswith("Interface "):
            current_iface = line.split()[-1]
            interfaces.append(current_iface)

    iface_data = []
    for iface in interfaces:
        info = {"interface": iface}

        # iw <iface> link
        out_link, rc_link = _run(["iw", iface, "link"])
        if rc_link == 0:
            for line in out_link.splitlines():
                line = line.strip()
                if line.startswith("SSID:"):
                    info["ssid"] = line.split(":", 1)[1].strip()
                elif line.startswith("signal:"):
                    info["signal_dbm"] = line.split(":", 1)[1].strip()
                elif line.startswith("tx bitrate:"):
                    info["tx_bitrate"] = line.split(":", 1)[1].strip()
                elif line.startswith("rx bitrate:"):
                    info["rx_bitrate"] = line.split(":", 1)[1].strip()
                elif "freq:" in line:
                    try:
                        freq = int(line.split("freq:")[1].strip().split()[0])
                        info["freq_mhz"] = freq
                        info["band"] = "5 GHz" if freq >= 5000 else "2.4 GHz"
                    except (ValueError, IndexError):
                        pass

        # iw <iface> station dump (for AP mode: connected clients)
        out_sta, rc_sta = _run(["iw", iface, "station", "dump"])
        if rc_sta == 0 and out_sta.strip():
            stations = []
            current_sta = {}
            for line in out_sta.splitlines():
                line = line.strip()
                if line.startswith("Station "):
                    if current_sta:
                        stations.append(current_sta)
                    current_sta = {"mac": line.split()[1]}
                elif "signal:" in line and current_sta:
                    current_sta["signal_dbm"] = line.split(":", 1)[1].strip()
                elif "tx bitrate:" in line and current_sta:
                    current_sta["tx_bitrate"] = line.split(":", 1)[1].strip()
                elif "connected time:" in line and current_sta:
                    current_sta["connected_time"] = line.split(":", 1)[1].strip()
            if current_sta:
                stations.append(current_sta)
            info["stations"] = stations
            info["station_count"] = len(stations)

        iface_data.append(info)

    return jsonify({"interfaces": iface_data})


@app.route("/api/system/cpu-governors")
@require_auth
def api_system_cpu_governors():
    """Return CPU frequency scaling governor and freq limits per core."""
    import glob as _glob
    cores = []
    for cpu_dir in sorted(_glob.glob("/sys/devices/system/cpu/cpu[0-9]*")):
        cpu_id = cpu_dir.split("/")[-1]
        freq_dir = cpu_dir + "/cpufreq"
        info = {"cpu": cpu_id}
        for fname in ("scaling_governor", "scaling_cur_freq", "scaling_min_freq",
                      "scaling_max_freq", "cpuinfo_min_freq", "cpuinfo_max_freq"):
            fpath = freq_dir + "/" + fname
            try:
                val = Path(fpath).read_text().strip()
                info[fname] = int(val) if val.isdigit() else val
            except OSError:
                pass
        if len(info) > 1:
            cores.append(info)

    # available governors (same for all cores — read from cpu0)
    avail = []
    try:
        avail = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors").read_text().strip().split()
    except OSError:
        pass

    return jsonify({"cores": cores, "available_governors": avail})
@app.route("/api/system/timers", methods=["GET"])
@require_auth
def api_system_timers():
    """Return systemd timers list with last/next trigger times."""
    try:
        out, rc = _run(
            ["systemctl", "list-timers", "--all", "--no-legend", "--no-pager"],
            timeout=10,
        )
        if rc != 0:
            return jsonify({"error": "systemctl unavailable", "timers": [], "count": 0})
        timers = []
        for line in (out or "").strip().splitlines():
            # format: NEXT LEFT LAST PASSED UNIT ACTIVATES
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            timers.append({
                "unit": parts[4],
                "activates": parts[5],
                "next": parts[0] if parts[0] != "n/a" else "",
                "left": parts[1] if parts[1] != "n/a" else "",
                "last": parts[2] if parts[2] != "n/a" else "",
                "passed": parts[3] if parts[3] != "n/a" else "",
            })
        return jsonify({"timers": timers, "count": len(timers), "source": "systemctl"})
    except Exception as exc:
        return jsonify({"error": str(exc), "timers": [], "count": 0})


@app.route("/api/network/mdns")
@require_auth
def api_network_mdns():
    """Browse mDNS/Avahi services on the local network."""
    # Try avahi-browse first
    out, rc = _run(["avahi-browse", "-a", "-t", "-r", "-p"])
    if rc != 0:
        # Fallback: try dns-sd
        out, rc = _run(["dns-sd", "-B", "_services._dns-sd._udp", "local"])
        if rc != 0:
            return jsonify({"services": [], "error": "avahi-browse and dns-sd unavailable"})

    services = []
    seen = set()
    for line in out.strip().splitlines():
        # avahi-browse -p format: =;iface;IPv4;name;type;domain;hostname;addr;port;txt
        if not line.startswith("="):
            continue
        parts = line.split(";")
        if len(parts) < 9:
            continue
        key = (parts[3], parts[4], parts[6])
        if key in seen:
            continue
        seen.add(key)
        services.append({
            "name": parts[3],
            "type": parts[4],
            "domain": parts[5],
            "hostname": parts[6],
            "address": parts[7],
            "port": parts[8],
        })

    return jsonify({"services": services, "count": len(services)})

@app.route("/api/system/failed-services")
@require_auth
def api_system_failed_services():
    """Return systemd units in failed state with recent log snippet."""
    out, _rc = _run(["systemctl", "list-units", "--state=failed",
                     "--no-pager", "--plain"])
    services = []
    for line in (out or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        unit_name = parts[0]
        svc = {
            "unit": unit_name,
            "load": parts[1] if len(parts) > 1 else "",
            "active": parts[2] if len(parts) > 2 else "",
            "sub": parts[3] if len(parts) > 3 else "",
            "description": parts[4] if len(parts) > 4 else "",
            "recent_log": [],
        }
        log_out, log_rc = _run(["systemctl", "status", unit_name,
                                 "--no-pager", "-n", "5"])
        if log_rc in (0, 3) and log_out:
            svc["recent_log"] = log_out.strip().splitlines()[-5:]
        services.append(svc)
        if len(services) >= 20:
            break
    count = len(services)
    return jsonify({"services": services, "count": count, "has_failures": count > 0})


@app.route("/api/network/ip-geo")
@require_auth
def api_network_ip_geo():
    """Return public IP and geolocation from ip-api.com (no key required)."""
    import urllib.request as _urlreq
    import json as _json
    try:
        req = _urlreq.Request(
            "http://ip-api.com/json/?fields=status,message,country,regionName,city,isp,org,as,query",
            headers={"User-Agent": "pi-travel-router/2.x"},
        )
        with _urlreq.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode())
        if data.get("status") != "success":
            return jsonify({"error": data.get("message", "lookup failed"), "ip": None})
        return jsonify({
            "ip": data.get("query"),
            "country": data.get("country"),
            "region": data.get("regionName"),
            "city": data.get("city"),
            "isp": data.get("isp"),
            "org": data.get("org"),
            "asn": data.get("as"),
        })
    except Exception as exc:  # pylint: disable=broad-except
        return jsonify({"error": str(exc), "ip": None})


@app.route("/api/system/ntp-peers")
@require_auth
def api_system_ntp_peers():
    """Return NTP peer status from chronyc or ntpq."""
    # Try chronyc first
    out, rc = _run(["chronyc", "sources", "-v"])
    if rc == 0:
        peers = []
        for line in out.splitlines():
            line = line.strip()
            # lines starting with * + - ? are peer entries
            if not line or line[0] not in ("*", "+", "-", "?", "x", "~"):
                continue
            parts = line.split()
            if len(parts) < 7:
                continue
            peers.append({
                "state": line[0],
                "source": parts[1],
                "stratum": parts[2],
                "poll": parts[3],
                "reach": parts[4],
                "last_rx": parts[5],
                "offset_ms": parts[6],
            })
        tracking = {}
        out_t, rc_t = _run(["chronyc", "tracking"])
        if rc_t == 0:
            for line in out_t.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    tracking[k.strip()] = v.strip()
        return jsonify({"backend": "chrony", "peers": peers, "tracking": tracking})

    # Fallback: ntpq
    out, rc = _run(["ntpq", "-p", "-n"])
    if rc == 0:
        peers = []
        for line in out.splitlines():
            if line.startswith("     ") or line.startswith("=") or not line.strip():
                continue
            state = line[0] if line[0] in ("*", "+", "-", "o", "x", "#", ".") else " "
            parts = line[1:].split()
            if len(parts) < 8:
                continue
            peers.append({
                "state": state,
                "source": parts[0],
                "stratum": parts[2],
                "poll": parts[4],
                "reach": parts[5],
                "offset_ms": parts[7],
            })
        return jsonify({"backend": "ntpq", "peers": peers})

    return jsonify({"backend": None, "peers": [], "error": "neither chrony nor ntpq available"})


@app.route("/api/system/hardware")
@require_auth
def api_system_hardware():
    """Return Raspberry Pi hardware info: model, revision, serial, memory."""
    result = {}

    # /proc/cpuinfo — model, revision, serial, hardware
    try:
        content = Path("/proc/cpuinfo").read_text()
        for line in content.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower().replace(" ", "_")
            val = val.strip()
            if key in ("model_name", "hardware", "revision", "serial", "model"):
                result[key] = val
    except OSError:
        pass

    # /proc/device-tree/model (Pi-specific)
    try:
        result["board_model"] = Path("/proc/device-tree/model").read_text().rstrip("\x00")
    except OSError:
        pass

    # /sys/firmware/devicetree/base/model (alternate path)
    if "board_model" not in result:
        try:
            result["board_model"] = Path("/sys/firmware/devicetree/base/model").read_text().rstrip("\x00")
        except OSError:
            pass

    # RAM total from /proc/meminfo
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                result["mem_total_kb"] = int(line.split()[1])
                break
    except (OSError, ValueError):
        pass

    # SD card info
    out, rc = _run(["cat", "/sys/block/mmcblk0/device/name"])
    if rc == 0:
        result["storage_device"] = out.strip()
    out2, rc2 = _run(["cat", "/sys/block/mmcblk0/size"])
    if rc2 == 0:
        try:
            result["storage_sectors"] = int(out2.strip())
            result["storage_bytes"] = result["storage_sectors"] * 512
        except ValueError:
            pass

    # vcgencmd (VideoCore GPU — Pi only)
    out_vc, rc_vc = _run(["vcgencmd", "get_throttled"])
    if rc_vc == 0:
        result["throttle_hex"] = out_vc.strip()

    return jsonify(result)


@app.route("/api/system/vmstat")
@require_auth
def api_system_vmstat():
    """Return VM statistics from /proc/vmstat — paging, swapping, faults."""
    _FIELDS = [
        "pgpgin", "pgpgout", "pswpin", "pswpout",
        "pgfault", "pgmajfault", "pgalloc_normal", "pgfree",
        "oom_kill", "nr_dirty", "nr_writeback",
        "numa_hit", "numa_miss",
        "thp_fault_alloc", "thp_collapse_alloc",
    ]
    result = {}
    try:
        content = Path("/proc/vmstat").read_text()
    except OSError:
        return jsonify({"error": "cannot read /proc/vmstat"})

    for line in content.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in _FIELDS:
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                pass

    # also grab /proc/sys/vm/swappiness and dirty_ratio
    for key, path in [
        ("swappiness", "/proc/sys/vm/swappiness"),
        ("dirty_ratio", "/proc/sys/vm/dirty_ratio"),
        ("dirty_background_ratio", "/proc/sys/vm/dirty_background_ratio"),
        ("overcommit_memory", "/proc/sys/vm/overcommit_memory"),
    ]:
        try:
            result[key] = int(Path(path).read_text().strip())
        except (OSError, ValueError):
            pass

    return jsonify(result)


@app.route("/api/system/uptime-history")
@require_auth
def api_system_uptime_history():
    """Return reboot/shutdown history from `last` plus current uptime from /proc/uptime."""
    events = []
    for kind in ("reboot", "shutdown"):
        out, rc = _run(["last", kind, "-n", "10"])
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                # Skip footer lines like "wtmp begins ..."
                if line.startswith("wtmp"):
                    continue
                # first token should match the kind
                first = line.split()[0] if line.split() else ""
                if first != kind:
                    continue
                events.append({"type": kind, "line": line})

    # current uptime from /proc/uptime
    uptime_seconds = None
    try:
        uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError):
        pass

    return jsonify({"events": events, "uptime_seconds": uptime_seconds})


@app.route("/api/network/socket-summary")
@require_auth
def api_network_socket_summary():
    """Return socket statistics summary from ss -s, falling back to /proc/net/sockstat."""
    out, rc = _run(["ss", "-s"])
    if rc == 0:
        result = {}
        for line in out.splitlines():
            line = line.strip()
            # "Total: 123" or "Total: 123 (kernel 456)"
            if line.startswith("Total:"):
                m = line.split()
                try:
                    result["total"] = int(m[1])
                except (IndexError, ValueError):
                    pass
            # "TCP:   10 (estab 3, closed 2, orphaned 0, timewait 1)"
            elif line.startswith("TCP:"):
                parts = line.split(":", 1)
                if len(parts) == 2:
                    rest = parts[1].strip()
                    nums = rest.split("(")[0].strip()
                    try:
                        result["tcp_total"] = int(nums)
                    except ValueError:
                        pass
                    # parse parenthesised states
                    for m in re.finditer(r'(\w+)\s+(\d+)', rest):
                        key, val = m.group(1), m.group(2)
                        if key in ("estab", "closed", "orphaned", "timewait"):
                            result["tcp_" + key] = int(val)
            # "UDP:   5"
            elif line.startswith("UDP:"):
                parts = line.split()
                try:
                    result["udp"] = int(parts[1])
                except (IndexError, ValueError):
                    pass
            # "RAW:   0"
            elif line.startswith("RAW:"):
                parts = line.split()
                try:
                    result["raw"] = int(parts[1])
                except (IndexError, ValueError):
                    pass
        result["source"] = "ss"
        return jsonify(result)

    # Fallback: parse /proc/net/sockstat
    result = {"source": "sockstat"}
    try:
        content = Path("/proc/net/sockstat").read_text()
    except OSError:
        return jsonify({"error": "cannot read socket statistics"})

    for line in content.splitlines():
        parts = line.split()
        if not parts:
            continue
        label = parts[0].rstrip(":")
        # Build a dict of key/value pairs from the rest
        kv = {}
        i = 1
        while i < len(parts) - 1:
            try:
                kv[parts[i]] = int(parts[i + 1])
            except ValueError:
                pass
            i += 2
        if label == "sockets":
            result["total"] = kv.get("used", 0)
        elif label == "TCP":
            result["tcp_total"] = kv.get("inuse", 0)
            result["tcp_timewait"] = kv.get("tw", 0)
            result["tcp_orphaned"] = kv.get("orphan", 0)
        elif label == "UDP":
            result["udp"] = kv.get("inuse", 0)
        elif label == "RAW":
            result["raw"] = kv.get("inuse", 0)

    return jsonify(result)


@app.route("/api/system/block-devices")
@require_auth
def api_system_block_devices():
    """Return block devices via lsblk -J with per-device I/O stats; fallback to /proc/partitions."""

    def _read_io_stats(name):
        """Read /sys/block/<name>/stat for I/O counters."""
        stat_path = Path("/sys/block") / name / "stat"
        try:
            fields = stat_path.read_text().split()
            return {
                "reads": int(fields[0]),
                "writes": int(fields[4]),
                "read_sectors": int(fields[2]),
                "write_sectors": int(fields[6]),
            }
        except (OSError, IndexError, ValueError):
            return None

    def _bool_field(val):
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val in ("1", "true", "True")
        return bool(val) if val is not None else False

    def _build_device(node, top_level=False):
        name = node.get("name") or ""
        children_raw = node.get("children") or []
        children = [_build_device(c) for c in children_raw]
        return {
            "name": name,
            "size": node.get("size") or "",
            "type": node.get("type") or "",
            "mountpoint": node.get("mountpoint") or None,
            "fstype": node.get("fstype") or None,
            "model": (node.get("model") or "").strip() or None,
            "vendor": (node.get("vendor") or "").strip() or None,
            "transport": node.get("tran") or None,
            "hotplug": _bool_field(node.get("hotplug")),
            "rotational": _bool_field(node.get("rota")),
            "state": node.get("state") or None,
            "children": children,
            "io_stats": _read_io_stats(name) if top_level else None,
        }

    lsblk_cmd = [
        "lsblk", "-J", "-o",
        "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,MODEL,SERIAL,VENDOR,TRAN,HOTPLUG,ROTA,PHY-SEC,LOG-SEC,STATE",
    ]
    out, rc = _run(lsblk_cmd)
    if rc == 0 and out.strip():
        try:
            data = json.loads(out)
            raw_devs = data.get("blockdevices", [])
            devices = [_build_device(d, top_level=True) for d in raw_devs]
            return jsonify({"devices": devices, "count": len(devices)})
        except (ValueError, KeyError):
            pass

    # Fallback: parse /proc/partitions
    proc_part = Path("/proc/partitions")
    if proc_part.exists():
        devices = []
        try:
            for line in proc_part.read_text().splitlines()[2:]:
                parts = line.split()
                if len(parts) >= 4:
                    name = parts[3]
                    if name.startswith("loop") or name.startswith("ram"):
                        continue
                    devices.append({
                        "name": name,
                        "size": "",
                        "type": "disk",
                        "mountpoint": None,
                        "fstype": None,
                        "model": None,
                        "vendor": None,
                        "transport": None,
                        "hotplug": False,
                        "rotational": None,
                        "state": None,
                        "children": [],
                        "io_stats": _read_io_stats(name),
                    })
        except OSError:
            pass
        return jsonify({"devices": devices, "count": len(devices)})

    return jsonify({"devices": [], "count": 0, "error": "lsblk not available"})


@app.route("/api/network/dns-config")
@require_auth
def api_network_dns_config():
    try:
        nameservers: list[str] = []
        search_domains: list[str] = []
        resolv_conf_path = "/etc/resolv.conf"
        try:
            for line in Path(resolv_conf_path).read_text().splitlines():
                line = line.strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2:
                        nameservers.append(parts[1])
                elif line.startswith("search"):
                    search_domains = line.split()[1:]
        except OSError:
            pass
        # resolvectl status for per-interface DNS
        interfaces: list[dict] = []
        out, rc = _run(["resolvectl", "status", "--no-pager"])
        if rc == 0:
            current_iface: str | None = None
            iface_dns: list[str] = []
            iface_domain: str | None = None
            for line in out.splitlines():
                # "Link 2 (eth0)" or "Link 3 (wlan0)"
                if line.startswith("Link "):
                    if current_iface is not None:
                        interfaces.append({
                            "name": current_iface,
                            "dns_servers": iface_dns,
                            "dns_domain": iface_domain,
                        })
                    import re as _re
                    m = _re.search(r'\(([^)]+)\)', line)
                    current_iface = m.group(1) if m else line.split()[1]
                    iface_dns = []
                    iface_domain = None
                elif current_iface and "DNS Servers:" in line:
                    iface_dns = line.split("DNS Servers:")[-1].split()
                elif current_iface and "DNS Domain:" in line:
                    iface_domain = line.split("DNS Domain:")[-1].strip() or None
            if current_iface is not None:
                interfaces.append({
                    "name": current_iface,
                    "dns_servers": iface_dns,
                    "dns_domain": iface_domain,
                })
        # Count non-comment, non-blank /etc/hosts entries
        hosts_entries = 0
        try:
            for line in Path("/etc/hosts").read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    hosts_entries += 1
        except OSError:
            pass
        return jsonify({
            "nameservers": nameservers,
            "search_domains": search_domains,
            "resolv_conf_path": resolv_conf_path,
            "interfaces": interfaces,
            "hosts_entries": hosts_entries,
            "source": "resolv.conf+resolvectl",
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "nameservers": [], "search_domains": []})


@app.route("/api/network/ip-rules")
@require_auth
def api_network_ip_rules():
    out, rc = _run(["ip", "rule", "show"])
    if rc != 0:
        return jsonify({"rules": [], "raw": None, "error": "ip rule show failed"})
    rules = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # "0:      from all lookup local"
        # "32766:  from all lookup main"
        parts = line.split(":", 1)
        try:
            priority = int(parts[0].strip())
        except ValueError:
            priority = None
        rest = parts[1].strip() if len(parts) > 1 else line
        # Extract table from "lookup <table>"
        table = None
        if "lookup " in rest:
            table = rest.split("lookup ")[-1].strip().split()[0]
        rules.append({"priority": priority, "rule": rest, "table": table})
    return jsonify({"rules": rules, "raw": out})


@app.route("/api/system/log-summary")
@require_auth
def api_system_log_summary():
    out, rc = _run(["journalctl", "-n", "100", "--no-pager", "-o", "short"])
    if rc != 0:
        return jsonify({"error": "journalctl failed", "total": 0, "errors": 0, "warnings": 0, "recent_errors": []})
    lines = out.splitlines()
    errors = [l for l in lines if ": err" in l.lower() or " error" in l.lower() or "[error]" in l.lower()]
    warnings = [l for l in lines if "warning" in l.lower() or "warn" in l.lower()]
    return jsonify({
        "total": len(lines),
        "errors": len(errors),
        "warnings": len(warnings),
        "recent_errors": errors[-5:],
    })


@app.route("/api/network/wifi-clients")
@require_auth
def api_network_wifi_clients():
    """Return clients connected to the Pi's AP via iw dev <iface> station dump."""
    ifaces = []
    out_dev, rc_dev = _run(["iw", "dev"])
    if rc_dev == 0:
        for line in out_dev.splitlines():
            line = line.strip()
            if line.startswith("Interface "):
                ifaces.append(line.split()[1])
    if not ifaces:
        ifaces = ["wlan0"]

    for iface in ifaces:
        out, rc = _run(["iw", "dev", iface, "station", "dump"])
        if rc == 0 and "Station" in out:
            clients = []
            current = {}
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Station "):
                    if current:
                        clients.append(current)
                    current = {"mac": line.split()[1]}
                elif ":" in line and current:
                    key, _, val = line.partition(":")
                    key = key.strip().lower().replace(" ", "_")
                    current[key] = val.strip()
            if current:
                clients.append(current)
            return jsonify({"clients": clients, "interface": iface})

    return jsonify({"clients": [], "interface": None})


@app.route("/api/system/package-updates")
@require_auth
def api_system_package_updates():
    """Return list of available package updates using apt-get dry-run."""
    def _parse_inst_lines(text, security_set):
        pkgs = []
        for line in text.splitlines():
            if not line.startswith("Inst "):
                continue
            # Inst PACKAGE [OLD] (NEW ...) or Inst PACKAGE (NEW ...)
            m = re.match(
                r"^Inst (\S+)"
                r"(?: \[([^\]]+)\])?"
                r"(?: \((\S+))?",
                line,
            )
            if not m:
                continue
            name = m.group(1)
            old_ver = m.group(2)
            new_ver = m.group(3)
            pkgs.append({
                "name": name,
                "old_version": old_ver,
                "new_version": new_ver,
                "is_security": name in security_set,
            })
        return pkgs

    error = None
    packages = []
    security_count = 0
    dist_upgrade_count = 0
    last_update = None

    try:
        out_upg, rc1 = _run(
            ["bash", "-c", "apt-get -s upgrade 2>/dev/null | grep '^Inst '"],
            timeout=30,
        )
        out_sec, _rc2 = _run(
            ["bash", "-c",
             "apt-get -s upgrade 2>/dev/null | grep -i security | grep '^Inst '"],
            timeout=30,
        )
        out_dist, _rc3 = _run(
            ["bash", "-c", "apt-get -s dist-upgrade 2>/dev/null | grep '^Inst '"],
            timeout=30,
        )

        security_names = {
            line.split()[1]
            for line in out_sec.splitlines()
            if line.startswith("Inst ")
        }

        packages = _parse_inst_lines(out_upg, security_names)
        security_count = sum(1 for p in packages if p["is_security"])

        dist_pkgs = _parse_inst_lines(out_dist, security_names)
        dist_upgrade_count = len(dist_pkgs)

        if rc1 != 0 and not packages:
            error = "apt-get not available or failed"
    except Exception as exc:  # pylint: disable=broad-except
        error = str(exc)

    try:
        apt_lists = Path("/var/lib/apt/lists/")
        if apt_lists.exists():
            mtime = apt_lists.stat().st_mtime
            last_update = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
    except Exception:  # pylint: disable=broad-except
        pass

    return jsonify({
        "upgradable": packages,
        "count": len(packages),
        "security_count": security_count,
        "dist_upgrade_count": dist_upgrade_count,
        "last_update": last_update,
        "error": error,
    })


@app.route("/api/network/bandwidth")
@require_auth
def api_network_bandwidth():
    """Return per-interface RX/TX byte counters from /proc/net/dev (cumulative since boot)."""
    interfaces = []
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()
    except OSError:
        return jsonify({"error": "Cannot read /proc/net/dev", "interfaces": [], "count": 0,
                        "timestamp": time.time()})

    # Skip first two header lines
    for line in lines[2:]:
        line = line.strip()
        if not line:
            continue
        colon = line.index(":")
        name = line[:colon].strip()
        if name == "lo":
            continue
        fields = line[colon + 1:].split()
        if len(fields) < 16:
            continue
        # /proc/net/dev columns:
        # RX: bytes packets errs drop fifo frame compressed multicast
        # TX: bytes packets errs drop fifo colls carrier compressed
        rx_bytes = int(fields[0])
        rx_packets = int(fields[1])
        rx_errors = int(fields[2])
        rx_dropped = int(fields[3])
        tx_bytes = int(fields[8])
        tx_packets = int(fields[9])
        tx_errors = int(fields[10])
        tx_dropped = int(fields[11])
        interfaces.append({
            "name": name,
            "rx_bytes": rx_bytes,
            "rx_packets": rx_packets,
            "rx_errors": rx_errors,
            "rx_dropped": rx_dropped,
            "tx_bytes": tx_bytes,
            "tx_packets": tx_packets,
            "tx_errors": tx_errors,
            "tx_dropped": tx_dropped,
            "rx_human": _fmt_bytes(rx_bytes),
            "tx_human": _fmt_bytes(tx_bytes),
        })

    interfaces.sort(key=lambda i: -(i["rx_bytes"] + i["tx_bytes"]))
    return jsonify({"interfaces": interfaces, "count": len(interfaces), "timestamp": time.time()})


@app.route("/api/network/iptables")
@require_auth
def api_network_iptables():
    def parse_chains(output):
        chains = {}
        current = None
        lines = []
        for line in output.splitlines():
            if line.startswith("Chain "):
                if current:
                    chains[current] = "\n".join(lines)
                parts = line.split()
                current = parts[1].lower()
                lines = [line]
            elif current:
                lines.append(line)
        if current:
            chains[current] = "\n".join(lines)
        return chains

    out4, rc4 = _run(["iptables", "-L", "-n", "-v", "--line-numbers"])
    ipv4 = parse_chains(out4) if rc4 == 0 else None
    out6, rc6 = _run(["ip6tables", "-L", "-n", "-v", "--line-numbers"])
    ipv6 = parse_chains(out6) if rc6 == 0 else None
    return jsonify({"ipv4": ipv4, "ipv6": ipv6})


@app.route("/api/system/thermal-history")
@require_auth
def api_thermal_history():
    """Return current thermal zone readings and GPU temp."""
    zones = []
    thermal_base = Path("/sys/class/thermal")
    if thermal_base.exists():
        for zone_dir in sorted(thermal_base.iterdir()):
            if not zone_dir.name.startswith("thermal_zone"):
                continue
            try:
                raw = (zone_dir / "temp").read_text().strip()
                temp_c = round(int(raw) / 1000.0, 1)
            except (OSError, ValueError):
                continue
            try:
                zone_type = (zone_dir / "type").read_text().strip()
            except OSError:
                zone_type = zone_dir.name
            zones.append({"zone": zone_dir.name, "type": zone_type, "temp_c": temp_c})

    gpu_temp_c = None
    vcgencmd_out, vcgencmd_rc = _run(["vcgencmd", "measure_temp"])
    if vcgencmd_rc == 0:
        m = re.search(r"temp=([\d.]+)", vcgencmd_out)
        if m:
            try:
                gpu_temp_c = round(float(m.group(1)), 1)
            except ValueError:
                pass

    result = {"zones": zones, "timestamp": time.time()}
    if gpu_temp_c is not None:
        result["gpu_temp_c"] = gpu_temp_c
    return jsonify(result)


# ── Process Tree ──────────────────────────────────────────────────────────────

@app.route("/api/system/process-tree")
@require_auth
def api_process_tree():
    out, rc = _run(
        ["ps", "-eo", "pid,ppid,user,%cpu,%mem,comm", "--sort=-%cpu", "--no-headers"]
    )
    processes = []
    for line in out.splitlines()[:20]:
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        processes.append({
            "pid":     int(parts[0]),
            "ppid":    int(parts[1]),
            "user":    parts[2],
            "cpu":     float(parts[3]),
            "mem":     float(parts[4]),
            "command": parts[5],
        })
    return jsonify({"processes": processes, "count": len(processes)})


# ── Network Neighbors (ARP/NDP) ───────────────────────────────────────────────


@app.route("/api/network/neighbors")
@require_auth
def api_network_neighbors():
    """Return ARP/NDP neighbor table parsed from `ip neigh show`."""

    # Regex: <ip> dev <iface> [lladdr <mac>] <STATE> [<extra>]
    _LINE_RE = re.compile(
        r"^(?P<ip>\S+)\s+dev\s+(?P<iface>\S+)"
        r"(?:\s+lladdr\s+(?P<mac>[0-9a-fA-F:]+))?"
        r"\s+(?P<state>[A-Z]+)"
    )
    _SKIP_STATES = {"FAILED", "INCOMPLETE"}

    def _parse_neigh(output):
        entries = {}
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _LINE_RE.match(line)
            if not m:
                continue
            state = m.group("state")
            if state in _SKIP_STATES:
                continue
            mac = m.group("mac")
            if not mac:
                continue
            ip = m.group("ip")
            entries[ip] = {
                "ip": ip,
                "mac": mac,
                "interface": m.group("iface"),
                "state": state,
            }
        return entries

    neighbors = {}

    out4, rc4 = _run(["ip", "neigh", "show"])
    if rc4 == 0:
        neighbors.update(_parse_neigh(out4))

    out6, rc6 = _run(["ip", "-6", "neigh", "show"])
    if rc6 == 0:
        for ip, entry in _parse_neigh(out6).items():
            if ip not in neighbors:
                neighbors[ip] = entry

    result = sorted(neighbors.values(), key=lambda e: e["ip"])
    return jsonify({"neighbors": result, "count": len(result)})


@app.route("/api/system/kernel-messages")
@require_auth
def api_system_kernel_messages():
    """Return recent kernel error/warn messages parsed from dmesg."""
    # Regex for dmesg -T timestamps: [Mon Jan  1 00:00:00 2024]
    _TS_RE = re.compile(r"^\[([^\]]+)\]\s+(\S+):\s+(.*)")
    _TS_BARE_RE = re.compile(r"^\[([^\]]+)\]\s+(.*)")
    # Level keywords for fallback classification
    _LEVEL_KEYWORDS = {
        "emerg": ["emerg"],
        "alert": ["alert"],
        "crit": ["crit"],
        "err": ["error", " err "],
        "warn": ["warn"],
    }

    def _classify_level(text):
        tl = text.lower()
        for lvl, keywords in _LEVEL_KEYWORDS.items():
            for kw in keywords:
                if kw in tl:
                    return lvl
        return "warn"

    def _parse_lines(output, has_timestamps):
        messages = []
        for line in output.splitlines()[-50:]:
            line = line.strip()
            if not line:
                continue
            if has_timestamps:
                m = _TS_RE.match(line)
                if m:
                    messages.append({
                        "timestamp": m.group(1).strip(),
                        "level": m.group(2).strip(" :[]").lower(),
                        "message": m.group(3).strip(),
                    })
                else:
                    m2 = _TS_BARE_RE.match(line)
                    if m2:
                        messages.append({
                            "timestamp": m2.group(1).strip(),
                            "level": _classify_level(m2.group(2)),
                            "message": m2.group(2).strip(),
                        })
                    else:
                        messages.append({
                            "timestamp": "",
                            "level": _classify_level(line),
                            "message": line,
                        })
            else:
                messages.append({
                    "timestamp": "",
                    "level": _classify_level(line),
                    "message": line,
                })
        return messages

    out, rc = _run(["dmesg", "--level=err,warn,crit,alert,emerg", "-T", "--no-pager"])
    if rc == 0:
        messages = _parse_lines(out, has_timestamps=True)
        return jsonify({"messages": messages, "count": len(messages)})

    # Fallback: dmesg without -T (no human-readable timestamps)
    out2, rc2 = _run(["dmesg", "-l", "err,warn,crit"])
    if rc2 == 0:
        messages = _parse_lines(out2, has_timestamps=False)
        return jsonify({"messages": messages, "count": len(messages)})

    return jsonify({"messages": [], "count": 0, "error": "dmesg unavailable"})


# ── Network Interfaces ─────────────────────────────────────────────────────────

@app.route("/api/network/interfaces")
@require_auth
def api_network_interfaces():
    def _parse_text(output):
        interfaces = []
        current = None
        _iface_re = re.compile(r"^\d+:\s+(\S+?)(?:@\S+)?:\s+<([^>]*)>\s+mtu\s+(\d+)")
        _link_re = re.compile(r"^\s+link/\S+\s+([0-9a-fA-F:]{17})")
        _addr_re = re.compile(r"^\s+(inet6?)\s+([0-9a-fA-F:.]+)/(\d+)")
        for line in output.splitlines():
            m = _iface_re.match(line)
            if m:
                if current is not None:
                    interfaces.append(current)
                flags = [f.strip() for f in m.group(2).split(",") if f.strip()]
                current = {"name": m.group(1), "flags": flags, "mtu": int(m.group(3)),
                           "mac": None, "addresses": []}
                continue
            if current is None:
                continue
            m = _link_re.match(line)
            if m:
                current["mac"] = m.group(1)
                continue
            m = _addr_re.match(line)
            if m:
                current["addresses"].append(
                    {"family": m.group(1), "addr": m.group(2), "prefix_len": int(m.group(3))})
        if current is not None:
            interfaces.append(current)
        return interfaces

    def _from_json(data):
        interfaces = []
        for iface in data:
            addresses = [{"family": ai.get("family", ""), "addr": ai.get("local", ""),
                          "prefix_len": ai.get("prefixlen", 0)}
                         for ai in (iface.get("addr_info") or [])]
            interfaces.append({"name": iface.get("ifname", ""), "flags": iface.get("flags") or [],
                                "mtu": iface.get("mtu", 0), "mac": iface.get("address"),
                                "addresses": addresses})
        return interfaces

    out, rc = _run(["ip", "-j", "addr", "show"])
    if rc == 0:
        try:
            return jsonify({"interfaces": _from_json(json.loads(out)), "count": len(json.loads(out))})
        except (ValueError, KeyError):
            pass
    out, rc = _run(["ip", "addr", "show"])
    interfaces = _parse_text(out) if rc == 0 else []
    return jsonify({"interfaces": interfaces, "count": len(interfaces)})


# ── Open Sockets Summary ──────────────────────────────────────────────────────

@app.route("/api/system/open-sockets", methods=["GET"])
@require_auth
def api_system_open_sockets():
    """Return open sockets grouped by protocol using ss -tunap."""
    out, rc = _run(["ss", "-tunap"])
    sockets = []
    if rc == 0:
        lines = out.splitlines()
        for line in lines[1:]:  # skip header
            parts = line.split()
            if len(parts) < 6:
                continue
            proto = parts[0]
            state = parts[1]
            local = parts[4]
            peer = parts[5]
            process = " ".join(parts[6:]) if len(parts) > 6 else ""
            sockets.append({
                "proto": proto,
                "state": state,
                "local": local,
                "peer": peer,
                "process": process,
            })
    # Count by protocol
    counts = {}
    for s in sockets:
        counts[s["proto"]] = counts.get(s["proto"], 0) + 1
    return jsonify({
        "sockets": sockets,
        "count": len(sockets),
        "by_proto": counts,
    })


# ── WiFi Signal Quality ───────────────────────────────────────────────────────

@app.route("/api/network/wifi-signal", methods=["GET"])
@require_auth
def api_network_wifi_signal():
    """Return WiFi signal strength for all wireless interfaces."""
    import os as _os

    # Find wireless interfaces
    wireless = []
    net_path = Path("/sys/class/net")
    try:
        for iface in sorted(net_path.iterdir()):
            if (iface / "wireless").exists() or (iface / "phy80211").exists():
                wireless.append(iface.name)
    except OSError:
        pass

    results = []
    for iface in wireless:
        info = {"interface": iface, "connected": False, "ssid": None,
                "signal_dbm": None, "signal_quality": None, "freq_mhz": None,
                "tx_bitrate": None, "rx_bitrate": None, "bssid": None}

        out, rc = _run(["iw", "dev", iface, "link"])
        if rc == 0 and "Not connected" not in out:
            info["connected"] = True
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("SSID:"):
                    info["ssid"] = line.split(":", 1)[1].strip()
                elif line.startswith("signal:"):
                    try:
                        info["signal_dbm"] = int(line.split()[1])
                        # Convert dBm to quality percent: quality = 2*(dBm+100), clamped 0-100
                        q = 2 * (info["signal_dbm"] + 100)
                        info["signal_quality"] = max(0, min(100, q))
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("freq:"):
                    try:
                        info["freq_mhz"] = int(line.split()[1])
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("tx bitrate:"):
                    info["tx_bitrate"] = " ".join(line.split()[2:4])
                elif line.startswith("rx bitrate:"):
                    info["rx_bitrate"] = " ".join(line.split()[2:4])
                elif line.startswith("Connected to"):
                    info["bssid"] = line.split()[2]

        results.append(info)

    # If no wireless ifaces found via sysfs, try iw dev list
    if not results:
        out, rc = _run(["iw", "dev"])
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Interface "):
                    iface = line.split()[1]
                    results.append({"interface": iface, "connected": False,
                                    "ssid": None, "signal_dbm": None,
                                    "signal_quality": None, "freq_mhz": None,
                                    "tx_bitrate": None, "rx_bitrate": None,
                                    "bssid": None})

    return jsonify({"interfaces": results, "count": len(results)})


# ── Cgroup Resource Stats ─────────────────────────────────────────────────────

@app.route("/api/system/cgroup-stats", methods=["GET"])
@require_auth
def api_system_cgroup_stats():
    """Return memory usage per systemd cgroup slice from cgroup v2 (or v1)."""
    import os as _os
    cgroups = []
    cgroup_root = Path("/sys/fs/cgroup")

    # cgroup v2: unified hierarchy
    if (cgroup_root / "memory.stat").exists() or (cgroup_root / "system.slice").exists():
        for name in ("system.slice", "user.slice"):
            base = cgroup_root / name
            if not base.is_dir():
                continue
            try:
                mem_current = (base / "memory.current").read_text().strip()
                mem_high = ""
                try:
                    mem_high = (base / "memory.high").read_text().strip()
                except OSError:
                    pass
                cpu_usage = ""
                try:
                    cpu_stat = (base / "cpu.stat").read_text()
                    for line in cpu_stat.splitlines():
                        if line.startswith("usage_usec"):
                            cpu_usage = line.split()[1]
                except OSError:
                    pass
                cgroups.append({
                    "name": name,
                    "mem_bytes": int(mem_current) if mem_current.isdigit() else 0,
                    "mem_high": mem_high if mem_high not in ("max", "") else "unlimited",
                    "cpu_usage_usec": int(cpu_usage) if cpu_usage.isdigit() else 0,
                    "version": 2,
                })
            except OSError:
                continue
        # Also enumerate services under system.slice
        services_dir = cgroup_root / "system.slice"
        if services_dir.is_dir():
            for svc_dir in sorted(services_dir.iterdir())[:20]:
                if not svc_dir.is_dir():
                    continue
                try:
                    mem_f = svc_dir / "memory.current"
                    if not mem_f.exists():
                        continue
                    mem_val = mem_f.read_text().strip()
                    mem_bytes = int(mem_val) if mem_val.isdigit() else 0
                    if mem_bytes == 0:
                        continue
                    cgroups.append({
                        "name": svc_dir.name,
                        "mem_bytes": mem_bytes,
                        "mem_high": "unknown",
                        "cpu_usage_usec": 0,
                        "version": 2,
                    })
                except OSError:
                    continue

    if not cgroups:
        # cgroup v1 fallback: /sys/fs/cgroup/memory/
        mem_root = Path("/sys/fs/cgroup/memory")
        if mem_root.is_dir():
            for entry in sorted(mem_root.iterdir())[:20]:
                if not entry.is_dir():
                    continue
                try:
                    usage_f = entry / "memory.usage_in_bytes"
                    limit_f = entry / "memory.limit_in_bytes"
                    if not usage_f.exists():
                        continue
                    usage = int(usage_f.read_text().strip())
                    limit_raw = limit_f.read_text().strip() if limit_f.exists() else ""
                    # max limit on 32-bit is 9223372036854771712
                    try:
                        limit = int(limit_raw)
                        limit_str = "unlimited" if limit > 2**62 else str(limit)
                    except ValueError:
                        limit_str = limit_raw
                    if usage == 0:
                        continue
                    cgroups.append({
                        "name": entry.name,
                        "mem_bytes": usage,
                        "mem_high": limit_str,
                        "cpu_usage_usec": 0,
                        "version": 1,
                    })
                except OSError:
                    continue

    def fmt_bytes(b):
        if b < 1024:
            return f"{b} B"
        elif b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        elif b < 1024 * 1024 * 1024:
            return f"{b / (1024 * 1024):.1f} MB"
        else:
            return f"{b / (1024 * 1024 * 1024):.2f} GB"

    for c in cgroups:
        c["mem_label"] = fmt_bytes(c["mem_bytes"])

    cgroups.sort(key=lambda x: -x["mem_bytes"])
    return jsonify({"cgroups": cgroups, "count": len(cgroups)})


# ── OOM Kill Events ───────────────────────────────────────────────────────────

@app.route("/api/system/oom-events", methods=["GET"])
@require_auth
def api_system_oom_events():
    """Return recent OOM kill events from dmesg/journal."""
    events = []

    # Try journalctl first (more reliable timestamps)
    out, rc = _run(["journalctl", "-k", "--no-pager", "-n", "500", "--output=short-iso"])
    if rc != 0:
        out, rc = _run(["dmesg", "-T"])

    if rc == 0:
        lines = out.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if "Out of memory" in line or "oom_kill_process" in line or "Killed process" in line:
                # Extract timestamp if present
                ts = ""
                ts_match = re.search(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', line)
                if ts_match:
                    ts = ts_match.group(1)
                # Extract process name and PID
                proc = ""
                pid = ""
                proc_match = re.search(r'[Kk]illed process (\d+) \(([^)]+)\)', line)
                if proc_match:
                    pid = proc_match.group(1)
                    proc = proc_match.group(2)
                # Extract memory info
                total_vm = ""
                anon_rss = ""
                vm_match = re.search(r'total-vm:(\d+)kB', line)
                if vm_match:
                    total_vm = vm_match.group(1)
                rss_match = re.search(r'anon-rss:(\d+)kB', line)
                if rss_match:
                    anon_rss = rss_match.group(1)
                events.append({
                    "timestamp": ts,
                    "process": proc,
                    "pid": pid,
                    "total_vm_kb": total_vm,
                    "anon_rss_kb": anon_rss,
                    "raw": line.strip()[-200:],
                })
            i += 1

    return jsonify({"events": events, "count": len(events), "oom_free": len(events) == 0})


# ── Hardware Sensors (Pi Firmware) ───────────────────────────────────────────

@app.route("/api/system/hardware-sensors", methods=["GET"])
@require_auth
def api_system_hardware_sensors():
    """Return Pi firmware sensor readings: voltage, clock speeds, throttle state."""
    sensors = {}

    # Throttle state (bitmask from firmware)
    out, rc = _run(["vcgencmd", "get_throttled"])
    if rc == 0:
        m = re.search(r'throttled=(0x[0-9a-fA-F]+)', out)
        if m:
            val = int(m.group(1), 16)
            sensors["throttled_hex"] = hex(val)
            sensors["throttled_flags"] = {
                "under_voltage": bool(val & 0x1),
                "arm_freq_capped": bool(val & 0x2),
                "currently_throttled": bool(val & 0x4),
                "soft_temp_limit": bool(val & 0x8),
                "under_voltage_occurred": bool(val & 0x10000),
                "arm_freq_capped_occurred": bool(val & 0x20000),
                "throttling_occurred": bool(val & 0x40000),
                "soft_temp_limit_occurred": bool(val & 0x80000),
            }

    # Core voltage
    out, rc = _run(["vcgencmd", "measure_volts", "core"])
    if rc == 0:
        m = re.search(r'volt=([\d.]+)V', out)
        if m:
            sensors["core_voltage_v"] = float(m.group(1))

    # SDRAM voltages
    for rail in ("sdram_c", "sdram_i", "sdram_p"):
        out, rc = _run(["vcgencmd", "measure_volts", rail])
        if rc == 0:
            m = re.search(r'volt=([\d.]+)V', out)
            if m:
                sensors[f"{rail}_voltage_v"] = float(m.group(1))

    # Clock speeds
    for clock in ("arm", "core", "h264", "isp", "v3d", "uart", "pwm", "emmc", "pixel", "vec", "hdmi", "dpi"):
        out, rc = _run(["vcgencmd", "measure_clock", clock])
        if rc == 0:
            m = re.search(r'frequency\(\d+\)=(\d+)', out)
            if m:
                sensors.setdefault("clocks_hz", {})[clock] = int(m.group(1))

    # GPU temperature
    out, rc = _run(["vcgencmd", "measure_temp"])
    if rc == 0:
        m = re.search(r'temp=([\d.]+)', out)
        if m:
            sensors["gpu_temp_c"] = float(m.group(1))

    # CPU temperature from sysfs (more reliable)
    try:
        for zone_path in sorted(Path("/sys/class/thermal").iterdir()):
            if not zone_path.name.startswith("thermal_zone"):
                continue
            temp_f = zone_path / "temp"
            type_f = zone_path / "type"
            if temp_f.exists():
                temp_val = int(temp_f.read_text().strip())
                zone_type = type_f.read_text().strip() if type_f.exists() else zone_path.name
                sensors.setdefault("cpu_temps", {})[zone_type] = round(temp_val / 1000, 1)
    except OSError:
        pass

    # Check if vcgencmd is available at all
    sensors["vcgencmd_available"] = bool(sensors.get("gpu_temp_c") is not None or "throttled_hex" in sensors)

    return jsonify(sensors)


# ── Firewall Rules (nftables/iptables) ───────────────────────────────────────

@app.route("/api/network/firewall-rules", methods=["GET"])
@require_auth
def api_network_firewall_rules():
    """Return active firewall rules from nftables (or iptables fallback)."""
    rules = []
    backend = "none"

    # Try nftables first
    out, rc = _run(["nft", "list", "ruleset"])
    if rc == 0 and out.strip():
        backend = "nftables"
        current_table = None
        current_chain = None
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("table "):
                parts = stripped.split()
                current_table = f"{parts[1]} {parts[2]}" if len(parts) >= 3 else stripped
                current_chain = None
            elif stripped.startswith("chain "):
                parts = stripped.split()
                current_chain = parts[1] if len(parts) >= 2 else stripped
            elif stripped and not stripped.startswith("}") and current_chain:
                # Skip hook/policy lines
                if stripped.startswith("type ") or stripped.startswith("policy "):
                    continue
                rules.append({
                    "table": current_table or "",
                    "chain": current_chain or "",
                    "rule": stripped,
                    "backend": "nftables",
                })
    else:
        # Fall back to iptables
        for table in ("filter", "nat", "mangle"):
            out, rc = _run(["iptables", "-t", table, "-L", "-n", "--line-numbers"])
            if rc != 0:
                continue
            backend = "iptables"
            current_chain = None
            for line in out.splitlines():
                if line.startswith("Chain "):
                    parts = line.split()
                    current_chain = parts[1] if len(parts) >= 2 else line
                elif line and not line.startswith("target") and not line.startswith("num") and current_chain:
                    parts = line.split()
                    if len(parts) >= 2:
                        rules.append({
                            "table": table,
                            "chain": current_chain,
                            "rule": line.strip(),
                            "backend": "iptables",
                        })

    # Group by table+chain
    chains = {}
    for rule in rules:
        key = f"{rule['table']}/{rule['chain']}"
        chains.setdefault(key, []).append(rule["rule"])

    return jsonify({
        "backend": backend,
        "rules": rules[:200],  # cap at 200 to avoid huge responses
        "chains": {k: v for k, v in list(chains.items())[:20]},
        "rule_count": len(rules),
        "chain_count": len(chains),
    })


# ── Traffic Shaping (tc) ─────────────────────────────────────────────────────

@app.route("/api/network/traffic-shaping", methods=["GET"])
@require_auth
def api_network_traffic_shaping():
    """Return tc qdisc stats for all network interfaces."""
    qdiscs = []

    out, rc = _run(["tc", "-s", "qdisc", "show"])
    if rc == 0:
        current = None
        for line in out.splitlines():
            line = line.strip()
            # New qdisc entry: "qdisc <type> <handle> dev <iface> ..."
            if line.startswith("qdisc "):
                if current is not None:
                    qdiscs.append(current)
                parts = line.split()
                qtype = parts[1] if len(parts) > 1 else ""
                handle = parts[2] if len(parts) > 2 else ""
                # Find "dev <iface>"
                dev = ""
                if "dev" in parts:
                    idx = parts.index("dev")
                    dev = parts[idx + 1] if idx + 1 < len(parts) else ""
                # Find "root" or "parent"
                root = "root" in parts
                parent = ""
                if "parent" in parts:
                    idx = parts.index("parent")
                    parent = parts[idx + 1] if idx + 1 < len(parts) else ""
                current = {
                    "type": qtype,
                    "handle": handle,
                    "dev": dev,
                    "root": root,
                    "parent": parent,
                    "sent_bytes": 0,
                    "sent_pkts": 0,
                    "dropped": 0,
                    "overlimits": 0,
                    "requeues": 0,
                    "backlog_bytes": 0,
                    "backlog_pkts": 0,
                }
            elif current is not None:
                # Stats line: "Sent X bytes Y pkts (dropped Z, overlimits W requeues R)"
                m = re.search(r'Sent\s+(\d+)\s+bytes\s+(\d+)\s+pkt', line)
                if m:
                    current["sent_bytes"] = int(m.group(1))
                    current["sent_pkts"] = int(m.group(2))
                m = re.search(r'dropped\s+(\d+)', line)
                if m:
                    current["dropped"] = int(m.group(1))
                m = re.search(r'overlimits\s+(\d+)', line)
                if m:
                    current["overlimits"] = int(m.group(1))
                m = re.search(r'requeues\s+(\d+)', line)
                if m:
                    current["requeues"] = int(m.group(1))
                m = re.search(r'backlog\s+(\d+)b\s+(\d+)p', line)
                if m:
                    current["backlog_bytes"] = int(m.group(1))
                    current["backlog_pkts"] = int(m.group(2))
        if current is not None:
            qdiscs.append(current)

    def fmt_bytes(b):
        if b < 1024:
            return f"{b} B"
        elif b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        elif b < 1024 * 1024 * 1024:
            return f"{b / (1024*1024):.1f} MB"
        else:
            return f"{b / (1024*1024*1024):.2f} GB"

    for q in qdiscs:
        q["sent_label"] = fmt_bytes(q["sent_bytes"])

    # Only include root qdiscs (one per interface) unless there are none
    root_qdiscs = [q for q in qdiscs if q["root"]]
    return jsonify({
        "qdiscs": root_qdiscs if root_qdiscs else qdiscs,
        "count": len(root_qdiscs if root_qdiscs else qdiscs),
        "tc_available": rc == 0,
    })


# ── Logged-In Sessions ────────────────────────────────────────────────────────

@app.route("/api/system/logged-in-sessions", methods=["GET"])
@require_auth
def api_system_logged_in_sessions():
    """Return currently logged-in user sessions."""
    sessions = []

    # Primary: loginctl list-sessions
    out, rc = _run(["loginctl", "list-sessions", "--no-legend"])
    if rc == 0 and out.strip():
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                session_id = parts[0]
                uid = parts[1]
                user = parts[2]
                seat = parts[3] if len(parts) > 3 else ""
                tty = parts[4] if len(parts) > 4 else ""
                # Get more detail per session
                detail_out, detail_rc = _run(["loginctl", "show-session", session_id,
                                              "--property=Type,Remote,RemoteHost,State,Service,Scope"])
                detail = {}
                if detail_rc == 0:
                    for dline in detail_out.splitlines():
                        if "=" in dline:
                            k, _, v = dline.partition("=")
                            detail[k.strip()] = v.strip()
                sessions.append({
                    "session_id": session_id,
                    "uid": uid,
                    "user": user,
                    "seat": seat,
                    "tty": tty,
                    "type": detail.get("Type", ""),
                    "remote": detail.get("Remote", "no").lower() == "yes",
                    "remote_host": detail.get("RemoteHost", ""),
                    "state": detail.get("State", ""),
                    "service": detail.get("Service", ""),
                })
    else:
        # Fallback: who -a
        out, rc = _run(["who", "-a"])
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0] not in ("system", "run-level", "boot"):
                    user = parts[0]
                    tty = parts[1] if len(parts) > 1 else ""
                    login_time = " ".join(parts[2:4]) if len(parts) >= 4 else ""
                    sessions.append({
                        "session_id": "",
                        "uid": "",
                        "user": user,
                        "seat": "",
                        "tty": tty,
                        "type": "tty",
                        "remote": False,
                        "remote_host": "",
                        "state": "active",
                        "service": "",
                        "login_time": login_time,
                    })

    return jsonify({"sessions": sessions, "count": len(sessions)})


# ── DNS Cache Stats ────────────────────────────────────────────────────────────

@app.route("/api/system/dns-cache", methods=["GET"])
@require_auth
def api_system_dns_cache():
    """Return DNS resolver cache statistics (systemd-resolved or dnsmasq)."""
    # Try systemd-resolved first
    out, rc = _run(["resolvectl", "statistics"])
    if rc == 0:
        stats = {
            "source": "systemd-resolved",
            "cache_size": None,
            "cache_hits": None,
            "cache_misses": None,
            "hit_rate_pct": None,
            "insertions": None,
            "evictions": None,
        }
        for line in out.splitlines():
            line = line.strip()
            # "Current Cache Size: 123"
            m = re.search(r'Current Cache Size:\s*(\d+)', line, re.IGNORECASE)
            if m:
                stats["cache_size"] = int(m.group(1))
            m = re.search(r'Cache Hits:\s*(\d+)', line, re.IGNORECASE)
            if m:
                stats["cache_hits"] = int(m.group(1))
            m = re.search(r'Cache Misses:\s*(\d+)', line, re.IGNORECASE)
            if m:
                stats["cache_misses"] = int(m.group(1))
        hits = stats["cache_hits"] or 0
        misses = stats["cache_misses"] or 0
        total = hits + misses
        stats["hit_rate_pct"] = round(hits * 100 / total, 1) if total > 0 else 0.0
        return jsonify(stats)

    # Try dnsmasq cache dump via SIGHUP-triggered log line
    # dnsmasq logs cache stats on SIGUSR1; check if dnsmasq is running
    out2, rc2 = _run(["pgrep", "-x", "dnsmasq"])
    if rc2 == 0:
        # Send SIGUSR1 to dump stats to syslog then read last line
        _run(["killall", "-USR1", "dnsmasq"])
        import time as _time
        _time.sleep(0.3)
        log_out, _ = _run(["journalctl", "-u", "dnsmasq", "-n", "20", "--no-pager", "--output=short"])
        cache_size = None
        insertions = None
        evictions = None
        for line in reversed(log_out.splitlines()):
            m = re.search(r'cache size (\d+)', line, re.IGNORECASE)
            if m and cache_size is None:
                cache_size = int(m.group(1))
            m = re.search(r'(\d+)/(\d+) cache insertions re-used unexpired cache entries', line)
            if m and insertions is None:
                insertions = int(m.group(1))
                evictions = int(m.group(2))
        return jsonify({
            "source": "dnsmasq",
            "cache_size": cache_size,
            "cache_hits": None,
            "cache_misses": None,
            "hit_rate_pct": None,
            "insertions": insertions,
            "evictions": evictions,
            "note": "Hit/miss stats not available from dnsmasq",
        })

    # Neither resolver available
    return jsonify({
        "source": "none",
        "cache_size": None,
        "cache_hits": None,
        "cache_misses": None,
        "hit_rate_pct": None,
        "insertions": None,
        "evictions": None,
        "note": "No supported DNS resolver found (tried systemd-resolved, dnsmasq)",
    })


# ── TCP Connection State Counts ──────────────────────────────────────────────

@app.route("/api/system/tcp-state-counts", methods=["GET"])
@require_auth
def api_system_tcp_state_counts():
    """Return TCP and UDP connection state breakdown."""
    # Use ss -tan for TCP states, ss -uan for UDP
    states = {}
    out, rc = _run(["ss", "-tan"])
    if rc == 0:
        for line in out.splitlines()[1:]:  # skip header
            parts = line.split()
            if parts:
                state = parts[0]
                states[state] = states.get(state, 0) + 1

    udp_count = 0
    out2, rc2 = _run(["ss", "-uan"])
    if rc2 == 0:
        udp_count = max(0, len(out2.splitlines()) - 1)

    # Also get listen counts
    listen_count = states.get("LISTEN", 0)
    established_count = states.get("ESTAB", 0)
    time_wait_count = states.get("TIME-WAIT", 0)
    close_wait_count = states.get("CLOSE-WAIT", 0)

    return jsonify({
        "states": states,
        "udp_count": udp_count,
        "listen": listen_count,
        "established": established_count,
        "time_wait": time_wait_count,
        "close_wait": close_wait_count,
        "total_tcp": sum(states.values()),
        "ss_available": rc == 0,
    })


# ── Pi Hardware Info ──────────────────────────────────────────────────────────

@app.route("/api/system/pi-hardware", methods=["GET"])
@require_auth
def api_system_pi_hardware():
    """Return Raspberry Pi hardware information from /proc/cpuinfo and vcgencmd."""
    info = {
        "model": None,
        "revision": None,
        "serial": None,
        "hardware": None,
        "memory_mb": None,
        "is_pi": False,
        "firmware_version": None,
        "bootloader_version": None,
        "arm_freq_mhz": None,
        "core_freq_mhz": None,
        "sdram_freq_mhz": None,
    }

    # Parse /proc/cpuinfo
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text()
        for line in cpuinfo.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key == "model name" and info["model"] is None:
                info["model"] = val
            elif key == "hardware":
                info["hardware"] = val
                if val.startswith("BCM") or "Pi" in cpuinfo:
                    info["is_pi"] = True
            elif key == "revision":
                info["revision"] = val
            elif key == "serial":
                info["serial"] = val
        # Also check for "Model" line (newer kernels)
        for line in cpuinfo.splitlines():
            if line.startswith("Model"):
                _, _, val = line.partition(":")
                info["model"] = val.strip()
                if "Raspberry Pi" in val:
                    info["is_pi"] = True
                break
    except OSError:
        pass

    # Memory total from /proc/meminfo
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                info["memory_mb"] = round(kb / 1024)
                break
    except OSError:
        pass

    # vcgencmd readings (Pi-only)
    fw_out, fw_rc = _run(["vcgencmd", "version"])
    if fw_rc == 0:
        info["is_pi"] = True
        for line in fw_out.splitlines():
            if line.strip():
                info["firmware_version"] = line.strip()
                break

    # Clock frequencies
    for clock, key in [("arm", "arm_freq_mhz"), ("core", "core_freq_mhz"), ("sdram_c", "sdram_freq_mhz")]:
        out, rc = _run(["vcgencmd", "measure_clock", clock])
        if rc == 0:
            m = re.search(r'=(\d+)', out)
            if m:
                info[key] = round(int(m.group(1)) / 1_000_000)

    # Bootloader version
    bl_out, bl_rc = _run(["rpi-eeprom-update"])
    if bl_rc == 0:
        for line in bl_out.splitlines():
            if "BOOTLOADER:" in line or "Current bootloader" in line.lower():
                info["bootloader_version"] = line.strip()
                break

    return jsonify(info)


# ── DHCP Server Stats ─────────────────────────────────────────────────────────

@app.route("/api/network/dhcp-server-stats", methods=["GET"])
@require_auth
def api_network_dhcp_server_stats():
    """Return DHCP server statistics from dnsmasq lease file and process info."""
    import os

    stats = {
        "active_leases": 0,
        "total_leases": 0,
        "lease_file": None,
        "server_running": False,
        "pid": None,
        "uptime_seconds": None,
        "pool_size": None,
        "ranges": [],
        "source": "none",
    }

    # Check if dnsmasq is running
    pgrep_out, pgrep_rc = _run(["pgrep", "-x", "dnsmasq"])
    if pgrep_rc == 0:
        stats["server_running"] = True
        pids = pgrep_out.strip().splitlines()
        stats["pid"] = int(pids[0]) if pids else None

    # Try to find and parse the lease file
    lease_paths = [
        "/var/lib/misc/dnsmasq.leases",
        "/tmp/dhcp.leases",
        "/var/lib/dnsmasq/dnsmasq.leases",
    ]
    for lp in lease_paths:
        if Path(lp).exists():
            stats["lease_file"] = lp
            try:
                lines = Path(lp).read_text().splitlines()
                now = int(time.time())
                active = 0
                total = len(lines)
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 5:
                        try:
                            expires = int(parts[0])
                            if expires == 0 or expires > now:
                                active += 1
                        except ValueError:
                            active += 1
                stats["total_leases"] = total
                stats["active_leases"] = active
                stats["source"] = "lease_file"
            except OSError:
                pass
            break

    # Parse dnsmasq config for DHCP ranges
    config_paths = [
        "/etc/dnsmasq.conf",
        "/etc/dnsmasq.d/travel-router.conf",
        "/etc/dnsmasq.d/02-dhcp.conf",
    ]
    ranges = []
    for cp in config_paths:
        if Path(cp).exists():
            try:
                for line in Path(cp).read_text().splitlines():
                    line = line.strip()
                    if line.startswith("dhcp-range="):
                        val = line[len("dhcp-range="):]
                        parts = val.split(",")
                        if len(parts) >= 2:
                            r = {"start": parts[0], "end": parts[1]}
                            if len(parts) >= 3:
                                r["netmask_or_tag"] = parts[2]
                            if len(parts) >= 4:
                                r["lease_time"] = parts[-1]
                            ranges.append(r)
            except OSError:
                pass
    stats["ranges"] = ranges

    # Estimate pool size from first range
    if ranges:
        try:
            import ipaddress
            r0 = ranges[0]
            start = ipaddress.IPv4Address(r0["start"])
            end = ipaddress.IPv4Address(r0["end"])
            stats["pool_size"] = int(end) - int(start) + 1
        except Exception:
            pass

    return jsonify(stats)


# ── Boot Time Analysis ────────────────────────────────────────────────────────

@app.route("/api/system/boot-analysis", methods=["GET"])
@require_auth
def api_system_boot_analysis():
    """Return systemd boot time analysis via systemd-analyze."""
    result = {
        "firmware_seconds": None,
        "loader_seconds": None,
        "kernel_seconds": None,
        "initrd_seconds": None,
        "userspace_seconds": None,
        "total_seconds": None,
        "graphical_target_seconds": None,
        "blame": [],
        "systemd_analyze_available": False,
    }

    # systemd-analyze time
    out, rc = _run(["systemd-analyze", "time"])
    if rc == 0:
        result["systemd_analyze_available"] = True
        for line in out.splitlines():
            line = line.strip()
            # "Startup finished in 1.234s (firmware) + 2.345s (loader) + ..."
            def _parse_sec(pattern):
                m = re.search(pattern, line)
                if m:
                    val = m.group(1)
                    # Convert to float seconds: "1.234s", "1min 2.345s", "2ms"
                    total = 0.0
                    min_m = re.search(r'(\d+)min', val)
                    if min_m:
                        total += int(min_m.group(1)) * 60
                    sec_m = re.search(r'([\d.]+)s', val)
                    if sec_m:
                        total += float(sec_m.group(1))
                    ms_m = re.search(r'([\d.]+)ms', val)
                    if ms_m:
                        total += float(ms_m.group(1)) / 1000
                    return round(total, 3)
                return None

            if "firmware" in line:
                result["firmware_seconds"] = _parse_sec(r'([\d.]+(?:min [\d.]+)?s) \(firmware\)')
            if "loader" in line:
                result["loader_seconds"] = _parse_sec(r'([\d.]+(?:min [\d.]+)?s) \(loader\)')
            if "kernel" in line:
                result["kernel_seconds"] = _parse_sec(r'([\d.]+(?:min [\d.]+)?s) \(kernel\)')
            if "initrd" in line:
                result["initrd_seconds"] = _parse_sec(r'([\d.]+(?:min [\d.]+)?s) \(initrd\)')
            if "userspace" in line:
                result["userspace_seconds"] = _parse_sec(r'([\d.]+(?:min [\d.]+)?s) \(userspace\)')
            if "graphical" in line.lower() or "reached" in line.lower():
                result["graphical_target_seconds"] = _parse_sec(r'([\d.]+(?:min [\d.]+)?s)')
            # Total from "= Xs" pattern
            m_total = re.search(r'= ([\d.]+(?:min [\d.]+)?s)\s*$', line)
            if m_total:
                result["total_seconds"] = _parse_sec(r'= ([\d.]+(?:min [\d.]+)?[sm].*?)(?:\s|$)')

    # systemd-analyze blame (top 15 slowest units)
    blame_out, blame_rc = _run(["systemd-analyze", "blame"])
    if blame_rc == 0:
        blame = []
        for line in blame_out.splitlines()[:15]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                time_str = parts[0]
                unit = parts[-1]
                # Parse time to float seconds
                total = 0.0
                min_m = re.search(r'(\d+)min', time_str)
                if min_m:
                    total += int(min_m.group(1)) * 60
                sec_m = re.search(r'([\d.]+)s', time_str)
                if sec_m:
                    total += float(sec_m.group(1))
                ms_m = re.search(r'([\d.]+)ms', time_str)
                if ms_m:
                    total += float(ms_m.group(1)) / 1000
                blame.append({"unit": unit, "seconds": round(total, 3), "time_str": time_str})
        result["blame"] = blame

    return jsonify(result)


# ── Network Link Status ───────────────────────────────────────────────────────

@app.route("/api/network/link-status", methods=["GET"])
@require_auth
def api_network_link_status():
    """Return physical link status for network interfaces via ip link show."""
    links = []

    # Get interface list from ip -j link show
    out, rc = _run(["ip", "-j", "link", "show"])
    if rc == 0:
        try:
            ifaces = json.loads(out)
        except (ValueError, KeyError):
            ifaces = []

        for iface in ifaces:
            name = iface.get("ifname", "")
            if name in ("lo",):
                continue
            flags = iface.get("flags", [])
            operstate = iface.get("operstate", "UNKNOWN").upper()
            carrier = "UP" in flags or operstate in ("UP", "LOWER_UP")
            link_type = iface.get("link_type", "")
            mtu = iface.get("mtu")
            mac = iface.get("address", "")

            entry = {
                "name": name,
                "operstate": operstate,
                "carrier": carrier,
                "mtu": mtu,
                "mac": mac,
                "link_type": link_type,
                "speed_mbps": None,
                "duplex": None,
                "port": None,
                "ethtool_available": False,
            }

            # Try ethtool for speed/duplex (may fail on virtual interfaces)
            eth_out, eth_rc = _run(["ethtool", name])
            if eth_rc == 0:
                entry["ethtool_available"] = True
                for line in eth_out.splitlines():
                    line = line.strip()
                    if line.startswith("Speed:"):
                        m = re.search(r'(\d+)\s*Mb/s', line)
                        if m:
                            entry["speed_mbps"] = int(m.group(1))
                    elif line.startswith("Duplex:"):
                        entry["duplex"] = line.split(":", 1)[1].strip()
                    elif line.startswith("Port:"):
                        entry["port"] = line.split(":", 1)[1].strip()

            links.append(entry)

    return jsonify({"links": links, "count": len(links)})

# ── User Accounts ─────────────────────────────────────────────────────────────

@app.route("/api/system/user-accounts", methods=["GET"])
@require_auth
def api_system_user_accounts():
    """Return non-system local user accounts from /etc/passwd."""
    users = []

    try:
        for line in Path("/etc/passwd").read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) < 7:
                continue
            username, _, uid, gid, gecos, home, shell = parts[:7]
            uid = int(uid)
            gid = int(gid)

            # Skip system accounts (uid < 1000) except root
            if uid != 0 and uid < 1000:
                continue
            # Skip nologin/false shell accounts (except root)
            if uid != 0 and shell in ("/usr/sbin/nologin", "/bin/false", "/sbin/nologin"):
                continue

            # Get supplementary groups
            groups = []
            try:
                for gline in Path("/etc/group").read_text().splitlines():
                    if not gline or gline.startswith("#"):
                        continue
                    gparts = gline.split(":")
                    if len(gparts) >= 4:
                        gname = gparts[0]
                        members = gparts[3].split(",") if gparts[3] else []
                        if username in members:
                            groups.append(gname)
            except OSError:
                pass

            # Check if account is locked (via /etc/shadow)
            locked = False
            try:
                for sline in Path("/etc/shadow").read_text().splitlines():
                    if sline.startswith(username + ":"):
                        sparts = sline.split(":")
                        if len(sparts) >= 2:
                            pw = sparts[1]
                            locked = pw.startswith("!") or pw.startswith("*") or pw == ""
                        break
            except OSError:
                pass  # shadow not readable — normal for non-root

            # Last login via lastlog
            last_login = None
            last_out, last_rc = _run(["lastlog", "-u", username])
            if last_rc == 0:
                for ll in last_out.splitlines()[1:]:
                    ll = ll.strip()
                    if ll and "**Never logged in**" not in ll:
                        last_login = ll
                    break

            users.append({
                "username": username,
                "uid": uid,
                "gid": gid,
                "gecos": gecos,
                "home": home,
                "shell": shell,
                "groups": groups,
                "locked": locked,
                "last_login": last_login,
            })
    except OSError:
        pass

    return jsonify({"users": users, "count": len(users)})

@app.route("/api/system/process-top", methods=["GET"])
@require_auth
def api_system_process_top():
    """Return top 15 processes by CPU and top 15 by memory."""
    def parse_ps(output):
        results = []
        lines = output.strip().splitlines()
        for line in lines[1:]:  # skip header
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            try:
                results.append({
                    "user": parts[0],
                    "pid": int(parts[1]),
                    "cpu": float(parts[2]),
                    "mem": float(parts[3]),
                    "rss_kb": int(parts[5]),
                    "command": parts[10][:60],
                })
            except (ValueError, IndexError):
                continue
        return results

    try:
        out_cpu, rc_cpu = _run(["ps", "aux", "--sort=-%cpu"])
        out_mem, rc_mem = _run(["ps", "aux", "--sort=-%mem"])

        if rc_cpu != 0 and rc_mem != 0:
            return jsonify({"error": "ps command failed", "by_cpu": [], "by_mem": []})

        by_cpu = parse_ps(out_cpu)[:15] if rc_cpu == 0 else []
        by_mem = parse_ps(out_mem)[:15] if rc_mem == 0 else []

        return jsonify({"by_cpu": by_cpu, "by_mem": by_mem, "source": "ps"})
    except Exception as exc:
        return jsonify({"error": str(exc), "by_cpu": [], "by_mem": []})


@app.route("/api/network/arp-table", methods=["GET"])
@require_auth
def api_network_arp_table():
    """Return ARP/NDP neighbor table from `ip -j neigh show`, filtered to non-FAILED entries."""
    def _parse_ip_neigh_json(raw):
        entries = []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        for item in data:
            state = item.get("state", ["UNKNOWN"])
            if isinstance(state, list):
                state = state[0] if state else "UNKNOWN"
            state = state.upper()
            if state == "FAILED":
                continue
            entries.append({
                "ip": item.get("dst", ""),
                "mac": item.get("lladdr", ""),
                "interface": item.get("dev", ""),
                "state": state,
            })
        return entries

    def _parse_arp_text(raw):
        entries = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("Address") or line.startswith("?"):
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            ip = parts[0]
            mac = parts[2] if parts[2] != "<incomplete>" else ""
            iface = parts[4]
            state = "REACHABLE" if mac else "INCOMPLETE"
            entries.append({"ip": ip, "mac": mac, "interface": iface, "state": state})
        return entries

    try:
        out, rc = _run(["ip", "-j", "neigh", "show"], timeout=5)
        if rc == 0:
            entries = _parse_ip_neigh_json(out)
            if entries is not None:
                return jsonify({"entries": entries, "count": len(entries), "source": "ip-neigh"})
        # Fallback to arp -n
        out2, rc2 = _run(["arp", "-n"], timeout=5)
        if rc2 == 0:
            entries = _parse_arp_text(out2)
            return jsonify({"entries": entries, "count": len(entries), "source": "arp-n"})
        return jsonify({"error": "arp table unavailable", "entries": [], "count": 0})
    except Exception as exc:
        return jsonify({"error": str(exc), "entries": [], "count": 0})


@app.route("/api/system/mounts")
@require_auth
def api_system_mounts():
    PSEUDO_FS = {
        "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs",
        "devpts", "cgroup", "cgroup2", "pstore", "debugfs", "tracefs",
        "hugetlbfs", "mqueue",
    }
    try:
        out = _run(["df", "-h", "--output=source,fstype,size,used,avail,pcent,target"])
        mounts = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 7:
                continue
            source, fstype, size, used, avail, pcent = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
            target = " ".join(parts[6:])
            if fstype in PSEUDO_FS:
                continue
            try:
                use_pct = int(pcent.rstrip("%"))
            except ValueError:
                use_pct = 0
            mounts.append({
                "source": source,
                "fstype": fstype,
                "size": size,
                "used": used,
                "avail": avail,
                "use_pct": use_pct,
                "target": target,
            })
        return jsonify({"mounts": mounts, "count": len(mounts), "source": "df"})
    except Exception as exc:
        return jsonify({"error": str(exc), "mounts": [], "count": 0})


@app.route("/api/network/ipv6-addresses")
@require_auth
def api_ipv6_addresses():
    try:
        out, rc = _run(["ip", "-6", "-j", "addr", "show"], timeout=5)
        if rc != 0:
            return jsonify({"error": "ip command failed", "interfaces": [], "total_addresses": 0, "has_global": False})
        data = json.loads(out)
        interfaces = []
        total_addresses = 0
        has_global = False
        for iface in data:
            name = iface.get("ifname", "")
            if name == "lo":
                continue
            addr_info = iface.get("addr_info", [])
            addresses = []
            for a in addr_info:
                scope = a.get("scope", "")
                if scope == "global":
                    has_global = True
                addresses.append({
                    "address": a.get("local", ""),
                    "prefixlen": a.get("prefixlen", 0),
                    "scope": scope,
                    "dynamic": bool(a.get("dynamic", False)),
                    "deprecated": bool(a.get("deprecated", False)),
                })
            if addresses:
                interfaces.append({"name": name, "addresses": addresses})
                total_addresses += len(addresses)
        return jsonify({
            "interfaces": interfaces,
            "total_addresses": total_addresses,
            "has_global": has_global,
            "source": "ip-addr",
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "interfaces": [], "total_addresses": 0, "has_global": False})


@app.route("/api/system/failed-units", methods=["GET"])
@require_auth
def api_system_failed_units():
    """Return a list of failed systemd units."""
    try:
        out, rc = _run(
            ["systemctl", "list-units", "--state=failed", "--no-legend", "--no-pager"],
            timeout=10,
        )
        if rc != 0 or not out.strip():
            out, rc = _run(
                ["systemctl", "--failed", "--no-legend", "--no-pager"],
                timeout=10,
            )
        units = []
        for line in (out or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            unit = parts[0]
            load = parts[1]
            active = parts[2]
            sub = parts[3]
            description = parts[4] if len(parts) > 4 else ""
            units.append({
                "unit": unit,
                "load": load,
                "active": active,
                "sub": sub,
                "description": description,
            })
        count = len(units)
        return jsonify({
            "units": units,
            "count": count,
            "healthy": count == 0,
            "source": "systemctl",
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "units": [], "count": 0, "healthy": True})


# ── NTP / Time Sync Status (v2) ───────────────────────────────────────────────

@app.route("/api/system/ntp-status", methods=["GET"])
@require_auth
def api_system_ntp_status():
    """Return NTP/time sync status via timedatectl show and timedatectl timesync-status."""
    def _parse_duration_ms(val: str):
        """Parse strings like '123ms', '1.234s', '456us' into float milliseconds."""
        val = val.strip()
        try:
            if val.endswith("ms"):
                return float(val[:-2])
            if val.endswith("us"):
                return float(val[:-2]) / 1000.0
            if val.endswith("s"):
                return float(val[:-1]) * 1000.0
            return float(val)
        except (ValueError, AttributeError):
            return None

    try:
        result = {
            "ntp_enabled": False,
            "ntp_synced": False,
            "timezone": None,
            "local_time": None,
            "rtc_time": None,
            "ntp_server": None,
            "offset_ms": None,
            "delay_ms": None,
            "jitter_ms": None,
            "source": "timedatectl",
        }

        out, rc = _run(["timedatectl", "show", "--no-pager"], timeout=5)
        if rc == 0 and out:
            for line in out.splitlines():
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                if k == "NTP":
                    result["ntp_enabled"] = v == "yes"
                elif k == "NTPSynchronized":
                    result["ntp_synced"] = v == "yes"
                elif k == "Timezone":
                    result["timezone"] = v
                elif k == "TimeUSec":
                    try:
                        result["local_time"] = v
                    except Exception:
                        pass
                elif k == "RTCTimeUSec":
                    result["rtc_time"] = v if v else None
                elif k == "LocalRTC":
                    pass  # informational only

        if not result["local_time"]:
            result["local_time"] = datetime.now().isoformat()

        out2, rc2 = _run(["timedatectl", "timesync-status", "--no-pager"], timeout=5)
        if rc2 == 0 and out2:
            for line in out2.splitlines():
                line = line.strip()
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k = k.strip().lower()
                v = v.strip()
                if k == "server" or k == "server address":
                    result["ntp_server"] = v if v else None
                elif k == "offset":
                    result["offset_ms"] = _parse_duration_ms(v)
                elif k == "delay":
                    result["delay_ms"] = _parse_duration_ms(v)
                elif k == "jitter":
                    result["jitter_ms"] = _parse_duration_ms(v)

        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc), "ntp_synced": False, "ntp_enabled": False})


# ── CPU Frequency Scaling ─────────────────────────────────────────────────────

@app.route("/api/system/cpu-freq", methods=["GET"])
@require_auth
def api_system_cpu_freq():
    """Return CPU frequency scaling info from sysfs for all online CPUs."""
    import glob as _glob
    cores = []
    cpu_dirs = sorted(_glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"))
    for cpu_dir in cpu_dirs:
        core_id = int(Path(cpu_dir).parent.name.replace("cpu", ""))
        def _read(fname):
            try:
                return Path(f"{cpu_dir}/{fname}").read_text().strip()
            except OSError:
                return None
        cur_khz = _read("scaling_cur_freq") or _read("cpuinfo_cur_freq")
        min_khz = _read("scaling_min_freq")
        max_khz = _read("scaling_max_freq")
        governor = _read("scaling_governor")
        def _to_mhz(val):
            try:
                return int(val) / 1000.0
            except (TypeError, ValueError):
                return 0.0
        cores.append({
            "core": core_id,
            "cur_mhz": _to_mhz(cur_khz),
            "min_mhz": _to_mhz(min_khz),
            "max_mhz": _to_mhz(max_khz),
            "governor": governor or "",
        })
    if not cores:
        return jsonify({"error": "cpufreq sysfs not available", "cores": [], "core_count": 0})
    avg_mhz = sum(c["cur_mhz"] for c in cores) / len(cores)
    governor = cores[0]["governor"] if cores else ""
    return jsonify({
        "cores": cores,
        "avg_mhz": round(avg_mhz, 1),
        "governor": governor,
        "core_count": len(cores),
        "source": "sysfs",
    })


SYSCTL_KEYS = [
    ("net.ipv4.ip_forward", "IPv4 forwarding", "1"),
    ("net.ipv6.conf.all.forwarding", "IPv6 forwarding", None),
    ("net.ipv4.conf.all.rp_filter", "Reverse path filter", "1"),
    ("net.ipv4.conf.all.accept_redirects", "Accept ICMP redirects", "0"),
    ("net.ipv4.conf.all.send_redirects", "Send ICMP redirects", "0"),
    ("net.ipv4.conf.all.accept_source_route", "Accept source routing", "0"),
    ("net.ipv4.tcp_syncookies", "TCP SYN cookies", "1"),
    ("kernel.randomize_va_space", "ASLR", "2"),
    ("kernel.dmesg_restrict", "dmesg restriction", "1"),
    ("kernel.kptr_restrict", "kernel pointer restriction", "1"),
    ("net.ipv4.icmp_echo_ignore_broadcasts", "Ignore broadcast pings", "1"),
    ("net.ipv4.tcp_rfc1337", "TCP RFC 1337", "1"),
    ("fs.protected_hardlinks", "Protected hardlinks", "1"),
    ("fs.protected_symlinks", "Protected symlinks", "1"),
]


@app.route("/api/system/sysctl-security")
@require_auth
def api_sysctl_security():
    params = []
    try:
        for key, description, recommended in SYSCTL_KEYS:
            out, err, rc = _run(["sysctl", "-n", key])
            value = out.strip() if rc == 0 else "error"
            params.append({
                "key": key,
                "description": description,
                "value": value,
                "recommended": recommended,
            })
        return jsonify({"params": params, "count": len(params), "source": "sysctl"})
    except Exception as exc:
        return jsonify({"error": str(exc), "params": [], "count": 0})


@app.route("/api/network/packet-stats")
@require_auth
def api_network_packet_stats():
    try:
        interfaces = []
        with open("/proc/net/dev") as f:
            lines = f.readlines()
        for line in lines[2:]:  # skip two header lines
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            name = name.strip()
            if name == "lo":
                continue
            fields = rest.split()
            if len(fields) < 16:
                continue
            rx_bytes    = int(fields[0])
            rx_packets  = int(fields[1])
            rx_errors   = int(fields[2])
            rx_dropped  = int(fields[3])
            tx_bytes    = int(fields[8])
            tx_packets  = int(fields[9])
            tx_errors   = int(fields[10])
            tx_dropped  = int(fields[11])
            interfaces.append({
                "name":       name,
                "rx_bytes":   rx_bytes,
                "rx_packets": rx_packets,
                "rx_errors":  rx_errors,
                "rx_dropped": rx_dropped,
                "tx_bytes":   tx_bytes,
                "tx_packets": tx_packets,
                "tx_errors":  tx_errors,
                "tx_dropped": tx_dropped,
                "rx_mb":      round(rx_bytes / 1048576, 2),
                "tx_mb":      round(tx_bytes / 1048576, 2),
            })
        return jsonify({"interfaces": interfaces, "count": len(interfaces), "source": "proc-net-dev"})
    except Exception as exc:
        return jsonify({"error": str(exc), "interfaces": [], "count": 0})


@app.route("/api/system/i2c-devices")
@require_auth
def api_i2c_devices():
    try:
        buses_paths = sorted(Path("/dev").glob("i2c-*"))
        if not buses_paths:
            return jsonify({
                "buses": [],
                "total_devices": 0,
                "bus_count": 0,
                "source": "i2cdetect",
                "note": "no i2c buses found or i2cdetect not installed",
            })
        buses = []
        for dev_path in buses_paths:
            try:
                bus_num = int(dev_path.name.split("-", 1)[1])
            except (ValueError, IndexError):
                continue
            rc, out, _ = _run(["i2cdetect", "-y", "-r", str(bus_num)])
            devices = []
            if rc == 0:
                for line in out.splitlines():
                    # lines look like: "00: -- -- -- -- -- -- -- -- 08 -- ..."
                    if ":" not in line:
                        continue
                    _, cells = line.split(":", 1)
                    for token in cells.split():
                        if token == "--" or token.startswith("UU"):
                            continue
                        try:
                            addr_int = int(token, 16)
                            devices.append({"address": token.lower(), "address_int": addr_int})
                        except ValueError:
                            pass
            buses.append({
                "bus": bus_num,
                "device_path": str(dev_path),
                "devices": devices,
            })
        total = sum(len(b["devices"]) for b in buses)
        if total == 0:
            return jsonify({
                "buses": buses,
                "total_devices": 0,
                "bus_count": len(buses),
                "source": "i2cdetect",
                "note": "no i2c buses found or i2cdetect not installed",
            })
        return jsonify({
            "buses": buses,
            "total_devices": total,
            "bus_count": len(buses),
            "source": "i2cdetect",
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "buses": [], "total_devices": 0})


@app.route("/api/system/inotify-stats")
@require_auth
def api_inotify_stats():
    try:
        def _read_int(path):
            with open(path) as f:
                return int(f.read().strip())
        max_watches = _read_int("/proc/sys/fs/inotify/max_user_watches")
        max_instances = _read_int("/proc/sys/fs/inotify/max_user_instances")
        max_queued = _read_int("/proc/sys/fs/inotify/max_queued_events")
        out, _rc = _run(
            "grep -r 'inotify' /proc/*/fdinfo/ --include='*' -l 2>/dev/null | wc -l"
        )
        current_watchers = int(out.strip()) if out.strip().isdigit() else 0
        watch_pct = min(100.0, current_watchers / max_watches * 100) if max_watches else 0.0
        return jsonify({
            "max_watches": max_watches,
            "max_instances": max_instances,
            "max_queued_events": max_queued,
            "current_watchers": current_watchers,
            "watch_usage_pct": round(watch_pct, 2),
            "source": "proc-sysfs",
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "max_watches": 0, "current_watchers": 0})


# ── Thermal Zones ─────────────────────────────────────────────────────────────

@app.route("/api/system/thermal-zones")
@require_auth
def api_thermal_zones():
    try:
        zones = []
        thermal_base = Path("/sys/class/thermal")
        if thermal_base.exists():
            for zone_dir in sorted(thermal_base.glob("thermal_zone*")):
                try:
                    zone_id = int(zone_dir.name.replace("thermal_zone", ""))
                    zone_type = (zone_dir / "type").read_text().strip()
                    temp_mc = int((zone_dir / "temp").read_text().strip())
                    temp_c = round(temp_mc / 1000.0, 2)
                    temp_f = round(temp_c * 9 / 5 + 32, 2)
                    hot_threshold_c = None
                    trip_path = zone_dir / "trip_point_0_temp"
                    if trip_path.exists():
                        try:
                            hot_threshold_c = round(int(trip_path.read_text().strip()) / 1000.0, 2)
                        except (ValueError, OSError):
                            pass
                    if temp_c >= 80:
                        status = "hot"
                    elif temp_c >= 60:
                        status = "warm"
                    else:
                        status = "normal"
                    zones.append({
                        "id": zone_id,
                        "type": zone_type,
                        "temp_c": temp_c,
                        "temp_f": temp_f,
                        "hot_threshold_c": hot_threshold_c,
                        "status": status,
                    })
                except (ValueError, OSError):
                    continue
        vcgencmd_gpu_c = None
        out, rc = _run("vcgencmd measure_temp")
        if rc == 0 and out:
            import re as _re
            m = _re.search(r"temp=([\d.]+)", out)
            if m:
                vcgencmd_gpu_c = round(float(m.group(1)), 2)
        max_temp_c = max((z["temp_c"] for z in zones), default=0.0)
        return jsonify({
            "zones": zones,
            "count": len(zones),
            "max_temp_c": max_temp_c,
            "vcgencmd_gpu_c": vcgencmd_gpu_c,
            "source": "sysfs",
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "zones": [], "count": 0})


# ── Detailed Memory Info ──────────────────────────────────────────────────────

@app.route("/api/system/memory-detail", methods=["GET"])
@require_auth
def api_system_memory_detail():
    """Return detailed memory breakdown from /proc/meminfo."""
    try:
        fields = {
            "MemTotal": "total_kb",
            "MemFree": "free_kb",
            "MemAvailable": "available_kb",
            "Buffers": "buffers_kb",
            "Cached": "cached_kb",
            "SwapCached": "swap_cached_kb",
            "Active": "active_kb",
            "Inactive": "inactive_kb",
            "Shmem": "shmem_kb",
            "Slab": "slab_kb",
            "SReclaimable": "slab_reclaimable_kb",
            "SUnreclaim": "s_unreclaim_kb",
            "KernelStack": "kernel_stack_kb",
            "PageTables": "page_tables_kb",
            "Dirty": "dirty_kb",
            "Writeback": "writeback_kb",
            "AnonPages": "anon_pages_kb",
            "Mapped": "mapped_kb",
            "VmallocTotal": "vmalloc_total_kb",
            "VmallocUsed": "vmalloc_used_kb",
            "HugePages_Total": "hugepages_total",
        }
        parsed: dict = {}
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                key = key.strip()
                if key in fields:
                    parsed[key] = int(rest.split()[0]) if rest.split() else 0

        total_kb = parsed.get("MemTotal", 0)
        available_kb = parsed.get("MemAvailable", parsed.get("MemFree", 0))
        used_kb = total_kb - available_kb
        use_pct = (used_kb / total_kb * 100) if total_kb else 0.0

        result: dict = {
            "total_kb": total_kb,
            "free_kb": parsed.get("MemFree", 0),
            "available_kb": available_kb,
            "used_kb": used_kb,
            "use_pct": round(use_pct, 2),
            "buffers_kb": parsed.get("Buffers", 0),
            "cached_kb": parsed.get("Cached", 0),
            "active_kb": parsed.get("Active", 0),
            "inactive_kb": parsed.get("Inactive", 0),
            "slab_kb": parsed.get("Slab", 0),
            "slab_reclaimable_kb": parsed.get("SReclaimable", 0),
            "kernel_stack_kb": parsed.get("KernelStack", 0),
            "page_tables_kb": parsed.get("PageTables", 0),
            "dirty_kb": parsed.get("Dirty", 0),
            "anon_pages_kb": parsed.get("AnonPages", 0),
            "vmalloc_used_kb": parsed.get("VmallocUsed", 0),
            "hugepages_total": parsed.get("HugePages_Total", 0),
            "source": "proc-meminfo",
        }
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc), "total_kb": 0, "use_pct": 0})


@app.route("/api/system/containers")
@require_auth
def api_system_containers():
    """Return running containers from Docker and/or Podman."""
    try:
        containers = []
        docker_available = False
        podman_available = False

        def _parse_containers(output, runtime):
            results = []
            for line in output.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    import json as _json
                    c = _json.loads(line)
                    results.append({
                        "id": c.get("ID", "")[:12],
                        "image": c.get("Image", ""),
                        "command": (c.get("Command", "") or "")[:40],
                        "status": c.get("Status", ""),
                        "name": c.get("Names", ""),
                        "ports": c.get("Ports", ""),
                        "runtime": runtime,
                    })
                except Exception:
                    continue
            return results

        docker_out, docker_rc = _run('docker ps --format "{{json .}}" 2>/dev/null')
        if docker_rc == 0:
            docker_available = True
            containers.extend(_parse_containers(docker_out or "", "docker"))

        podman_out, podman_rc = _run('podman ps --format "{{json .}}" 2>/dev/null')
        if podman_rc == 0:
            podman_available = True
            containers.extend(_parse_containers(podman_out or "", "podman"))

        if not docker_available and not podman_available:
            return jsonify({
                "containers": [],
                "count": 0,
                "docker_available": False,
                "podman_available": False,
                "note": "no container runtime found",
            })

        return jsonify({
            "containers": containers,
            "count": len(containers),
            "docker_available": docker_available,
            "podman_available": podman_available,
            "source": "docker+podman",
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "containers": [], "count": 0})


@app.route("/api/system/login-history", methods=["GET"])
@require_auth
def api_system_login_history():
    """Return recent login history from `last` command."""

    def _parse_last_lines(lines):
        parsed = []
        for line in lines:
            line = line.rstrip()
            if not line or line.startswith("wtmp begins") or line.startswith("btmp begins"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            user = parts[0]
            tty = parts[1]
            # Determine 'from' field: if parts[2] looks like a date/dash, there is no from field
            idx = 2
            from_host = ""
            if not (parts[2][0].isdigit() or parts[2].startswith("-")):
                from_host = parts[2]
                idx = 3
            login_time = parts[idx] if idx < len(parts) else ""
            still_logged_in = False
            logout_time = None
            duration = None
            rest = " ".join(parts[idx + 1:]) if idx + 1 < len(parts) else ""
            if "still logged in" in rest or "logged in" in rest:
                still_logged_in = True
            else:
                dash_pos = rest.find(" - ")
                if dash_pos != -1:
                    after_dash = rest[dash_pos + 3:].strip()
                    after_parts = after_dash.split()
                    logout_time = after_parts[0] if after_parts else None
                    paren_start = rest.find("(")
                    paren_end = rest.find(")")
                    if paren_start != -1 and paren_end != -1:
                        duration = rest[paren_start + 1:paren_end]
            parsed.append({
                "user": user,
                "tty": tty,
                "host": from_host,
                "login_time": login_time,
                "logout_time": logout_time,
                "duration": duration,
                "still_logged_in": still_logged_in,
            })
        return parsed

    logins = []
    out, rc = _run(["last", "-n", "30", "--time-format", "iso"])
    if rc == 0:
        logins = _parse_last_lines(out.splitlines())

    failed_attempts = []
    try:
        out_b, rc_b = _run(["lastb", "-n", "10", "--time-format", "iso"])
        if rc_b == 0:
            failed_attempts = _parse_last_lines(out_b.splitlines())
    except Exception:
        pass

    still_logged_in_count = sum(1 for e in logins if e["still_logged_in"])
    return jsonify({
        "logins": logins,
        "count": len(logins),
        "failed_attempts": failed_attempts,
        "failed_count": len(failed_attempts),
        "still_logged_in": still_logged_in_count,
    })


# ── Open File Descriptors ─────────────────────────────────────────────────────

@app.route("/api/system/open-fds", methods=["GET"])
@require_auth
def api_system_open_fds():
    """Return open FD stats from /proc/sys/fs/file-nr, inode-nr, lsof, and top procs."""
    import subprocess as _sp
    error = None
    allocated = free = max_fds = 0
    inodes_allocated = inodes_free = None
    lsof_total = None
    top_procs = []
    try:
        # /proc/sys/fs/file-nr: allocated, unused(free), max
        nr = Path("/proc/sys/fs/file-nr").read_text().strip().split()
        if len(nr) >= 3:
            allocated = int(nr[0])
            free = int(nr[1])
            max_fds = int(nr[2])
    except Exception as exc:
        error = str(exc)

    # /proc/sys/fs/inode-nr: allocated inodes, free inodes
    try:
        inr = Path("/proc/sys/fs/inode-nr").read_text().strip().split()
        if len(inr) >= 2:
            inodes_allocated = int(inr[0])
            inodes_free = int(inr[1])
    except Exception:
        pass

    # lsof total open files (subtract 1 for header)
    try:
        result = _sp.run(
            ["lsof", "-s"],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().splitlines()
        lsof_total = max(0, len(lines) - 1)
    except Exception:
        lsof_total = None

    # top-10 processes by FD count via lsof
    try:
        result2 = _sp.run(
            ["lsof", "-s"],
            capture_output=True, text=True, timeout=10,
        )
        counts = {}
        for line in result2.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if parts:
                name = parts[0]
                counts[name] = counts.get(name, 0) + 1
        top_procs = [
            {"name": k, "count": v}
            for k, v in sorted(counts.items(), key=lambda x: -x[1])[:10]
        ]
    except Exception:
        top_procs = []

    pct_used = round((allocated - free) / max_fds * 100, 1) if max_fds > 0 else 0.0

    return jsonify({
        "allocated": allocated,
        "free": free,
        "max": max_fds,
        "pct_used": pct_used,
        "inodes_allocated": inodes_allocated,
        "inodes_free": inodes_free,
        "lsof_total": lsof_total,
        "top_procs": top_procs,
        "error": error,
    })


# ── Crontab ───────────────────────────────────────────────────────────────────

@app.route("/api/system/crontab", methods=["GET"])
@require_auth
def api_system_crontab():
    """Return parsed cron entries from all standard sources."""
    entries = []
    sources_seen = []

    def _parse_cron_line(line, source, has_user_field):
        """Parse a single cron line into a dict. Returns None if unparseable."""
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        parts = line.split()
        if len(parts) < 2:
            return None
        # Handle @-shortcuts like @reboot, @daily, etc.
        if parts[0].startswith("@"):
            schedule = parts[0]
            if has_user_field and len(parts) >= 3:
                user = parts[1]
                command = " ".join(parts[2:])
            else:
                user = ""
                command = " ".join(parts[1:])
            return {"schedule": schedule, "user": user, "command": command, "source": source}
        # Standard 5-field schedule
        if len(parts) < 6:
            return None
        schedule = " ".join(parts[:5])
        if has_user_field:
            if len(parts) < 7:
                return None
            user = parts[5]
            command = " ".join(parts[6:])
        else:
            user = ""
            command = " ".join(parts[5:])
        return {"schedule": schedule, "user": user, "command": command, "source": source}

    # root's personal crontab
    out, rc = _run("crontab -l 2>/dev/null")
    if rc == 0 and out.strip():
        source = "crontab"
        sources_seen.append(source)
        for line in out.splitlines():
            entry = _parse_cron_line(line, source, has_user_field=False)
            if entry:
                entries.append(entry)

    # /etc/crontab
    etc_crontab = Path("/etc/crontab")
    if etc_crontab.exists():
        source = "/etc/crontab"
        sources_seen.append(source)
        try:
            for line in etc_crontab.read_text().splitlines():
                entry = _parse_cron_line(line, source, has_user_field=True)
                if entry:
                    entries.append(entry)
        except OSError:
            pass

    # /etc/cron.d/*
    for cron_dir, has_user in [
        ("/etc/cron.d", True),
        ("/etc/cron.daily", False),
        ("/etc/cron.hourly", False),
        ("/etc/cron.weekly", False),
        ("/etc/cron.monthly", False),
    ]:
        cron_path = Path(cron_dir)
        if not cron_path.is_dir():
            continue
        try:
            files = sorted(cron_path.iterdir())
        except OSError:
            continue
        for f in files:
            if not f.is_file():
                continue
            source = str(f)
            try:
                text = f.read_text()
            except OSError:
                continue
            found_any = False
            for line in text.splitlines():
                entry = _parse_cron_line(line, source, has_user_field=has_user)
                if entry:
                    entries.append(entry)
                    found_any = True
            if found_any:
                sources_seen.append(source)

    return jsonify({
        "entries": entries,
        "total": len(entries),
        "sources": sources_seen,
    })


@app.route("/api/system/kernel-config")
@require_auth
def api_system_kernel_config():
    """Return selected kernel config keys from /boot/config-<uname-r> or /proc/config.gz."""
    KEYS = [
        # Security
        "CONFIG_SECURITY",
        "CONFIG_SECURITY_SELINUX",
        "CONFIG_SECURITY_APPARMOR",
        "CONFIG_SECCOMP",
        "CONFIG_HARDENED_USERCOPY",
        "CONFIG_RANDOMIZE_BASE",
        "CONFIG_RANDOMIZE_MEMORY",
        # Networking
        "CONFIG_NETFILTER",
        "CONFIG_NF_CONNTRACK",
        "CONFIG_BRIDGE",
        "CONFIG_VLAN_8021Q",
        "CONFIG_TUN",
        "CONFIG_WIREGUARD",
        "CONFIG_IPV6",
        # Pi-specific
        "CONFIG_BCM2835",
        "CONFIG_RASPBERRYPI_FIRMWARE",
        "CONFIG_USB_DWCOTG",
    ]

    config_text = None
    source = None

    # Try /boot/config-$(uname -r)
    uname_out, uname_rc = _run("uname -r")
    if uname_rc == 0:
        kernel_ver = uname_out.strip()
        boot_path = f"/boot/config-{kernel_ver}"
        try:
            config_text = Path(boot_path).read_text()
            source = boot_path
        except OSError:
            pass

    # Fallback: /proc/config.gz
    if config_text is None:
        gz_text, gz_rc = _run("zcat /proc/config.gz 2>/dev/null")
        if gz_rc == 0 and gz_text.strip():
            config_text = gz_text
            source = "/proc/config.gz"

    if config_text is None:
        return jsonify({"keys": {}, "source": None, "error": "kernel config not found"})

    # Parse key=value lines
    key_map: dict = {}
    pattern = re.compile(r"^(CONFIG_\w+)=(\S+)")
    not_set_pattern = re.compile(r"^# (CONFIG_\w+) is not set")
    for line in config_text.splitlines():
        m = pattern.match(line)
        if m:
            key_map[m.group(1)] = m.group(2).strip('"')
            continue
        m2 = not_set_pattern.match(line)
        if m2:
            key_map[m2.group(1)] = "n"

    result = {k: key_map.get(k) for k in KEYS}
    return jsonify({
        "keys": result,
        "source": source,
        "total_keys": sum(1 for v in result.values() if v is not None),
    })


# ── Network Bonding ───────────────────────────────────────────────────────────

@app.route("/api/network/bonding", methods=["GET"])
@require_auth
def api_network_bonding():
    """Return bonding interface status from /sys/class/net."""
    bonds = []
    net_base = Path("/sys/class/net")
    if net_base.is_dir():
        for iface_dir in sorted(net_base.iterdir()):
            bond_dir = iface_dir / "bonding"
            if not bond_dir.is_dir():
                continue
            name = iface_dir.name

            def _sysread(rel):
                try:
                    return (iface_dir / rel).read_text().strip()
                except OSError:
                    return ""

            mode = _sysread("bonding/mode")
            slaves_raw = _sysread("bonding/slaves")
            active_slave = _sysread("bonding/active_slave")
            miimon_raw = _sysread("bonding/miimon")
            operstate = _sysread("operstate")

            try:
                miimon = int(miimon_raw)
            except (ValueError, TypeError):
                miimon = 0

            slave_list = []
            for slave_name in slaves_raw.split() if slaves_raw else []:
                slave_dir = net_base / slave_name
                slave_operstate = ""
                slave_bond_state = ""
                try:
                    slave_operstate = (slave_dir / "operstate").read_text().strip()
                except OSError:
                    pass
                try:
                    slave_bond_state = (slave_dir / "bonding_slave" / "state").read_text().strip()
                except OSError:
                    pass
                slave_list.append({
                    "name": slave_name,
                    "state": slave_bond_state or slave_operstate,
                })

            bonds.append({
                "name": name,
                "mode": mode,
                "state": operstate,
                "active_slave": active_slave,
                "miimon": miimon,
                "slaves": slave_list,
            })

    return jsonify({"bonds": bonds, "count": len(bonds)})


# ── System Environment ────────────────────────────────────────────────────────

_ENV_SAFE_KEYS = {
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "USER", "HOME",
    "HOSTNAME", "TZ", "PYTHONPATH", "VIRTUAL_ENV", "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR", "SYSTEMD_EXEC_PID",
}

_ENV_SYSTEMD_EXTRA_KEYS = {
    "INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_UNIT_PATH",
}

_ENV_SECRET_PATTERNS = re.compile(
    r"PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL|AUTH", re.IGNORECASE
)


def _env_is_safe(key):
    """Return True if the key is allowed to be exposed."""
    return not _ENV_SECRET_PATTERNS.search(key)


@app.route("/api/system/environment", methods=["GET"])
@require_auth
def api_system_environment():
    """Return a filtered view of process and systemd environment variables."""
    variables = []

    # Source 1: current process environment
    process_allowed = _ENV_SAFE_KEYS
    for key, value in os.environ.items():
        if key in process_allowed and _env_is_safe(key):
            variables.append({"key": key, "value": value, "source": "process"})

    # Source 2: systemd / init environment from /proc/1/environ
    systemd_allowed = _ENV_SAFE_KEYS | _ENV_SYSTEMD_EXTRA_KEYS
    seen_keys = {v["key"] for v in variables}
    try:
        raw = Path("/proc/1/environ").read_bytes()
        for entry in raw.split(b"\x00"):
            if b"=" not in entry:
                continue
            try:
                decoded = entry.decode("utf-8", errors="replace")
            except Exception:
                continue
            key, _, value = decoded.partition("=")
            key = key.strip()
            if not key:
                continue
            if key not in systemd_allowed:
                continue
            if not _env_is_safe(key):
                continue
            source = "systemd"
            if key in seen_keys:
                # already have a process entry — skip duplicate
                continue
            variables.append({"key": key, "value": value, "source": source})
            seen_keys.add(key)
    except OSError:
        pass

    variables.sort(key=lambda v: v["key"])
    return jsonify({"variables": variables, "total": len(variables)})


# ── Network Socket Stats ──────────────────────────────────────────────────────

@app.route("/api/network/sockets", methods=["GET"])
@require_auth
def api_network_sockets():
    """Return network socket statistics from ss and /proc/net/sockstat."""
    summary = {"total": 0, "tcp_estab": 0, "tcp_timewait": 0, "udp": 0}
    tcp_states: dict = {}
    top_listeners: list = []
    sockstat: dict = {"sockets_used": 0, "tcp_alloc": 0, "udp_inuse": 0}

    # --- ss -s summary ---
    out, rc = _run(["ss", "-s"])
    if rc != 0:
        # fallback: netstat -s (best-effort, very limited)
        out, rc = _run(["netstat", "-s"])

    if rc == 0 and out:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Total:"):
                parts = line.split()
                try:
                    summary["total"] = int(parts[1])
                except (IndexError, ValueError):
                    pass
            elif line.startswith("TCP:"):
                rest = line.split(":", 1)[1].strip()
                for m in re.finditer(r'(\w+)\s+(\d+)', rest):
                    key, val = m.group(1), int(m.group(2))
                    if key == "estab":
                        summary["tcp_estab"] = val
                    elif key == "timewait":
                        summary["tcp_timewait"] = val
            elif line.startswith("UDP:"):
                parts = line.split()
                try:
                    summary["udp"] = int(parts[1])
                except (IndexError, ValueError):
                    pass

    # --- ss -tan for TCP state breakdown ---
    out2, rc2 = _run(["ss", "-tan"])
    if rc2 == 0:
        for line in out2.splitlines()[1:]:
            parts = line.split()
            if parts:
                state = parts[0]
                tcp_states[state] = tcp_states.get(state, 0) + 1

    # --- ss -tnp for top listeners ---
    out3, rc3 = _run(["ss", "-tnp"])
    if rc3 == 0:
        port_count: dict = {}
        port_proc: dict = {}
        for line in out3.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 5:
                continue
            # local address is parts[3]; format is addr:port
            local = parts[3]
            port_str = local.rsplit(":", 1)[-1]
            try:
                port = int(port_str)
            except ValueError:
                continue
            port_count[port] = port_count.get(port, 0) + 1
            # process name is in the last column: users:(("sshd",pid=1,fd=3))
            if port not in port_proc and len(parts) >= 6:
                proc_col = parts[-1]
                m = re.search(r'"([^"]+)"', proc_col)
                port_proc[port] = m.group(1) if m else ""
        # top 10 by connection count
        sorted_ports = sorted(port_count.items(), key=lambda x: x[1], reverse=True)[:10]
        top_listeners = [
            {"port": p, "process": port_proc.get(p, ""), "count": c}
            for p, c in sorted_ports
        ]

    # --- /proc/net/sockstat ---
    try:
        content = Path("/proc/net/sockstat").read_text()
        for line in content.splitlines():
            parts = line.split()
            if not parts:
                continue
            label = parts[0].rstrip(":")
            kv: dict = {}
            i = 1
            while i < len(parts) - 1:
                try:
                    kv[parts[i]] = int(parts[i + 1])
                except ValueError:
                    pass
                i += 2
            if label == "sockets":
                sockstat["sockets_used"] = kv.get("used", 0)
            elif label == "TCP":
                sockstat["tcp_alloc"] = kv.get("alloc", 0)
            elif label == "UDP":
                sockstat["udp_inuse"] = kv.get("inuse", 0)
    except OSError:
        pass

    return jsonify({
        "summary": summary,
        "tcp_states": tcp_states,
        "top_listeners": top_listeners,
        "sockstat": sockstat,
    })


# ── SMART Disk Health ─────────────────────────────────────────────────────────

@app.route("/api/system/smart", methods=["GET"])
@require_auth
def api_system_smart():
    """Return SMART disk health data for all block devices."""
    # Check if smartctl is available
    smartctl_check, smartctl_rc = _run(["which", "smartctl"])
    smartctl_available = smartctl_rc == 0

    if not smartctl_available:
        return jsonify({
            "disks": [],
            "smartctl_available": False,
            "error": "smartctl not found",
        })

    disks = []
    try:
        sys_block = Path("/sys/block")
        if not sys_block.exists():
            return jsonify({"disks": [], "smartctl_available": True})

        for dev_path in sorted(sys_block.iterdir()):
            name = dev_path.name
            # Skip virtual/pseudo devices
            if name.startswith(("loop", "ram", "zram")):
                continue

            device = f"/dev/{name}"
            out, rc = _run(["smartctl", "-H", "-A", device], timeout=15)

            disk_info = {
                "device": device,
                "health": None,
                "temp_c": None,
                "reallocated": None,
                "power_on_hours": None,
                "error": None,
            }

            if rc not in (0, 4):
                disk_info["error"] = f"smartctl exit {rc}"
                if not out.strip():
                    disks.append(disk_info)
                    continue

            for line in out.splitlines():
                # Overall health
                if "SMART overall-health self-assessment test result:" in line:
                    if "PASSED" in line:
                        disk_info["health"] = "PASSED"
                    elif "FAILED" in line:
                        disk_info["health"] = "FAILED"

                # SMART attributes
                parts = line.split()
                if len(parts) >= 10:
                    attr_name = parts[1]
                    raw_val = parts[9]

                    if attr_name in ("Temperature_Celsius", "Airflow_Temperature_Cel"):
                        try:
                            disk_info["temp_c"] = int(raw_val.split()[0])
                        except (ValueError, IndexError):
                            pass

                    elif attr_name == "Reallocated_Sector_Ct":
                        try:
                            disk_info["reallocated"] = int(raw_val)
                        except ValueError:
                            pass

                    elif attr_name == "Power_On_Hours":
                        try:
                            disk_info["power_on_hours"] = int(raw_val)
                        except ValueError:
                            pass

            disks.append(disk_info)

    except Exception as exc:
        return jsonify({"disks": disks, "smartctl_available": True, "error": str(exc)})

    return jsonify({"disks": disks, "smartctl_available": True})


# ── GPU / VideoCore Info ──────────────────────────────────────────────────────

@app.route("/api/system/gpu", methods=["GET"])
@require_auth
def api_gpu_info():
    """Return Raspberry Pi VideoCore GPU information via vcgencmd."""
    # Check if vcgencmd is available
    _, rc_check = _run("vcgencmd version")
    if rc_check != 0:
        return jsonify({"vcgencmd_available": False, "error": "vcgencmd not found — not a Raspberry Pi?"})

    def _parse_kv(output):
        """Parse 'key=value' output, returning value string or None."""
        if not output:
            return None
        m = re.search(r"=(.+)", output.strip())
        return m.group(1).strip() if m else output.strip()

    # ARM memory
    arm_mem_mb = None
    out, rc = _run("vcgencmd get_mem arm")
    if rc == 0 and out:
        m = re.search(r"(\d+)M", out)
        if m:
            arm_mem_mb = int(m.group(1))

    # GPU memory
    gpu_mem_mb = None
    out, rc = _run("vcgencmd get_mem gpu")
    if rc == 0 and out:
        m = re.search(r"(\d+)M", out)
        if m:
            gpu_mem_mb = int(m.group(1))

    # Core voltage
    core_voltage_v = None
    out, rc = _run("vcgencmd measure_volts core")
    if rc == 0 and out:
        m = re.search(r"volt=([\d.]+)V", out)
        if m:
            core_voltage_v = round(float(m.group(1)), 4)

    # SDRAM core voltage
    sdram_voltage_v = None
    out, rc = _run("vcgencmd measure_volts sdram_c")
    if rc == 0 and out:
        m = re.search(r"volt=([\d.]+)V", out)
        if m:
            sdram_voltage_v = round(float(m.group(1)), 4)

    # ARM clock
    arm_clock_hz = None
    out, rc = _run("vcgencmd measure_clock arm")
    if rc == 0 and out:
        m = re.search(r"frequency\(\d+\)=(\d+)", out)
        if m:
            arm_clock_hz = int(m.group(1))

    # Core clock
    core_clock_hz = None
    out, rc = _run("vcgencmd measure_clock core")
    if rc == 0 and out:
        m = re.search(r"frequency\(\d+\)=(\d+)", out)
        if m:
            core_clock_hz = int(m.group(1))

    # Throttle flags
    throttled_info = {"raw": None, "undervoltage": False, "freq_capped": False, "throttled": False, "undervoltage_occurred": False}
    out, rc = _run("vcgencmd get_throttled")
    if rc == 0 and out:
        m = re.search(r"throttled=(0x[0-9a-fA-F]+|\d+)", out)
        if m:
            raw_val = m.group(1)
            throttled_info["raw"] = raw_val
            flags = int(raw_val, 16) if raw_val.startswith("0x") else int(raw_val)
            throttled_info["undervoltage"] = bool(flags & (1 << 0))
            throttled_info["freq_capped"] = bool(flags & (1 << 1))
            throttled_info["throttled"] = bool(flags & (1 << 2))
            throttled_info["undervoltage_occurred"] = bool(flags & (1 << 16))

    # Config integers (best-effort, not required)
    config_ints = {}
    out, rc = _run("vcgencmd get_config int")
    if rc == 0 and out:
        for line in out.strip().splitlines():
            m = re.match(r"^([a-zA-Z0-9_]+)=(\d+)$", line.strip())
            if m:
                config_ints[m.group(1)] = int(m.group(2))

    return jsonify({
        "vcgencmd_available": True,
        "arm_mem_mb": arm_mem_mb,
        "gpu_mem_mb": gpu_mem_mb,
        "arm_clock_hz": arm_clock_hz,
        "core_clock_hz": core_clock_hz,
        "core_voltage_v": core_voltage_v,
        "sdram_voltage_v": sdram_voltage_v,
        "throttled": throttled_info,
        "config_ints": config_ints,
    })


@app.route("/api/network/wifi-survey")
def api_network_wifi_survey():
    """Available WiFi networks from iwlist scan."""
    try:
        # Find wireless interfaces
        iface_out, _ = _run("iw dev 2>/dev/null | awk '/Interface/{print $2}'")
        ifaces = [i.strip() for i in iface_out.strip().splitlines() if i.strip()]
        if not ifaces:
            return jsonify({"error": "No wireless interfaces found", "networks": [], "interface": None})
        # Use the first interface that looks like a client (not ap-only)
        iface = ifaces[0]
        scan_out, rc = _run(f"iwlist {iface} scan 2>&1")
        if rc != 0 or "Interface doesn't support scanning" in scan_out:
            # Try nmcli as fallback
            nm_out, nm_rc = _run("nmcli -t -f SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY dev wifi list 2>/dev/null")
            if nm_rc == 0 and nm_out.strip():
                networks = []
                for line in nm_out.strip().splitlines():
                    parts = line.split(":")
                    if len(parts) >= 6:
                        networks.append({
                            "ssid": parts[0] or "<hidden>",
                            "bssid": parts[1],
                            "channel": parts[2],
                            "frequency": parts[3],
                            "signal_dbm": int(parts[4]) if parts[4].lstrip("-").isdigit() else None,
                            "encryption": parts[5] if len(parts) > 5 else "Unknown",
                            "source": "nmcli",
                        })
                networks.sort(key=lambda x: x.get("signal_dbm") or -100, reverse=True)
                return jsonify({"networks": networks, "interface": iface, "count": len(networks)})
            return jsonify({"error": f"Scan failed on {iface}: {scan_out[:200]}", "networks": [], "interface": iface})
        # Parse iwlist output
        networks = []
        current = {}
        for line in scan_out.splitlines():
            line = line.strip()
            if line.startswith("Cell "):
                if current:
                    networks.append(current)
                # "Cell 01 - Address: AA:BB:CC:DD:EE:FF"
                addr_match = re.search(r"Address:\s+([0-9A-Fa-f:]{17})", line)
                current = {"bssid": addr_match.group(1) if addr_match else "", "source": "iwlist"}
            elif line.startswith("ESSID:"):
                ssid = re.sub(r'^ESSID:"?|"?$', "", line).strip('"')
                current["ssid"] = ssid or "<hidden>"
            elif line.startswith("Channel:"):
                current["channel"] = line.split(":", 1)[1].strip()
            elif line.startswith("Frequency:"):
                freq_match = re.search(r"Frequency:([\d.]+\s*GHz)", line)
                current["frequency"] = freq_match.group(1) if freq_match else line.split(":", 1)[1].split()[0]
            elif "Signal level=" in line:
                sig_match = re.search(r"Signal level=(-?\d+)", line)
                current["signal_dbm"] = int(sig_match.group(1)) if sig_match else None
            elif line.startswith("Encryption key:"):
                enc = line.split(":", 1)[1].strip()
                if enc == "off":
                    current.setdefault("encryption", "Open")
            elif "IE: IEEE 802.11i/WPA2" in line:
                current["encryption"] = "WPA2"
            elif "IE: WPA Version" in line:
                current.setdefault("encryption", "WPA")
        if current:
            networks.append(current)
        # Fill missing encryption
        for n in networks:
            n.setdefault("encryption", "Unknown")
        networks.sort(key=lambda x: x.get("signal_dbm") or -100, reverse=True)
        return jsonify({"networks": networks, "interface": iface, "count": len(networks)})
    except Exception as e:
        return jsonify({"error": str(e), "networks": []})


@app.route("/api/network/dhcp-leases")
def api_network_dhcp_leases():
    """Active DHCP leases from dnsmasq or dhcpd lease files."""
    leases = []
    source = None
    error = None

    # Try dnsmasq leases first
    dnsmasq_paths = [
        "/var/lib/misc/dnsmasq.leases",
        "/var/lib/dnsmasq/dnsmasq.leases",
        "/tmp/dnsmasq.leases",
    ]
    for path in dnsmasq_paths:
        try:
            content = Path(path).read_text()
            source = path
            for line in content.strip().splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    expires_ts = int(parts[0]) if parts[0].isdigit() else 0
                    mac = parts[1]
                    ip = parts[2]
                    hostname = parts[3] if parts[3] != "*" else ""
                    # Time remaining
                    now = int(time.time())
                    remaining = expires_ts - now if expires_ts > 0 else None
                    if remaining is not None and remaining < 0:
                        continue  # expired
                    if remaining is not None:
                        h = remaining // 3600
                        m = (remaining % 3600) // 60
                        expires_human = f"{h}h {m}m" if h > 0 else f"{m}m"
                    else:
                        expires_human = "permanent"
                    leases.append({
                        "expires_ts": expires_ts,
                        "expires_human": expires_human,
                        "mac": mac,
                        "ip": ip,
                        "hostname": hostname,
                    })
            break
        except (FileNotFoundError, PermissionError):
            continue
        except Exception as e:
            error = str(e)

    # Try isc-dhcp-server as fallback
    if not leases and not source:
        dhcpd_paths = ["/var/lib/dhcp/dhcpd.leases", "/var/lib/dhcpd/dhcpd.leases"]
        for path in dhcpd_paths:
            try:
                content = Path(path).read_text()
                source = path
                current = {}
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith("lease "):
                        current = {"ip": line.split()[1]}
                    elif "hardware ethernet" in line:
                        current["mac"] = line.split()[-1].rstrip(";")
                    elif "client-hostname" in line:
                        current["hostname"] = line.split('"')[1] if '"' in line else ""
                    elif line.startswith("ends "):
                        # ends 1 2024/01/15 12:00:00;
                        current["expires_human"] = " ".join(line.split()[1:]).rstrip(";")
                    elif line == "}" and "ip" in current:
                        leases.append(current.copy())
                        current = {}
                break
            except (FileNotFoundError, PermissionError):
                continue
            except Exception as e:
                error = str(e)

    leases.sort(key=lambda x: x.get("ip", ""))
    result = {"leases": leases, "count": len(leases), "source": source}
    if error:
        result["error"] = error
    if not source:
        result["error"] = "No DHCP lease file found (checked dnsmasq and dhcpd paths)"
    return jsonify(result)


@app.route("/api/system/processes")
def api_system_processes():
    """Top processes by CPU and memory usage."""
    try:
        # ps output: pid, %cpu, %mem, rss(KB), vsz(KB), stat, user, comm
        ps_out, rc = _run(
            "ps aux --no-headers --sort=-%cpu 2>/dev/null | head -30"
        )
        if rc != 0 or not ps_out.strip():
            ps_out, _ = _run("ps aux | tail -n +2 | head -30")

        processes = []
        for line in ps_out.strip().splitlines():
            parts = line.split(None, 10)
            if len(parts) < 10:
                continue
            try:
                proc = {
                    "user": parts[0],
                    "pid": int(parts[1]),
                    "cpu_pct": float(parts[2]),
                    "mem_pct": float(parts[3]),
                    "vsz_kb": int(parts[4]),
                    "rss_kb": int(parts[5]),
                    "tty": parts[6],
                    "stat": parts[7],
                    "start": parts[8],
                    "time": parts[9],
                    "command": parts[10][:80] if len(parts) > 10 else "",
                }
                processes.append(proc)
            except (ValueError, IndexError):
                continue

        # Summary
        total_cpu = sum(p["cpu_pct"] for p in processes)
        total_mem_pct = sum(p["mem_pct"] for p in processes)

        return jsonify({
            "processes": processes[:25],
            "total": len(processes),
            "summary": {
                "top_cpu_pct": total_cpu,
                "top_mem_pct": total_mem_pct,
            }
        })
    except Exception as e:
        return jsonify({"error": str(e), "processes": []})


@app.route("/api/network/connected-clients")
def api_network_connected_clients():
    """WiFi clients connected to the AP (hostapd) and all LAN clients."""
    result = {"ap_clients": [], "lan_clients": [], "ap_interface": None}

    # hostapd connected stations
    try:
        iface_out, _ = _run("iw dev 2>/dev/null | awk '/Interface/{print $2}'")
        ap_iface = None
        for iface in iface_out.strip().splitlines():
            iface = iface.strip()
            type_out, rc = _run(f"iw dev {iface} info 2>/dev/null | grep type")
            if rc == 0 and "AP" in type_out:
                ap_iface = iface
                break
        if ap_iface:
            result["ap_interface"] = ap_iface
            sta_out, rc = _run(f"iw dev {ap_iface} station dump 2>/dev/null")
            if rc == 0 and sta_out.strip():
                current = {}
                for line in sta_out.strip().splitlines():
                    line = line.strip()
                    if line.startswith("Station "):
                        if current:
                            result["ap_clients"].append(current)
                        mac = line.split()[1]
                        current = {"mac": mac}
                    elif "signal:" in line:
                        m = re.search(r"signal:\s+([-\d]+)", line)
                        if m:
                            current["signal_dbm"] = int(m.group(1))
                    elif "rx bytes:" in line:
                        m = re.search(r"rx bytes:\s+(\d+)", line)
                        if m:
                            current["rx_bytes"] = int(m.group(1))
                    elif "tx bytes:" in line:
                        m = re.search(r"tx bytes:\s+(\d+)", line)
                        if m:
                            current["tx_bytes"] = int(m.group(1))
                    elif "connected time:" in line:
                        m = re.search(r"connected time:\s+(\d+)", line)
                        if m:
                            secs = int(m.group(1))
                            h, rem = divmod(secs, 3600)
                            current["connected_time"] = f"{h}h {rem//60}m" if h else f"{rem//60}m"
                if current:
                    result["ap_clients"].append(current)
    except Exception as e:
        result["ap_error"] = str(e)

    # All LAN clients from ARP + neighbor table
    try:
        neigh_out, _ = _run("ip neigh show 2>/dev/null")
        seen_macs = {c["mac"] for c in result["ap_clients"]}
        lan = []
        for line in neigh_out.strip().splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            ip = parts[0]
            mac_idx = next((i for i, p in enumerate(parts) if re.match(r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$', p.lower())), None)
            if mac_idx is None:
                continue
            mac = parts[mac_idx]
            state = parts[-1]
            if state in ("FAILED", "INCOMPLETE"):
                continue
            iface = parts[2] if len(parts) > 2 else ""
            if mac not in seen_macs:
                lan.append({"ip": ip, "mac": mac, "state": state, "iface": iface})
                seen_macs.add(mac)
        result["lan_clients"] = lan
    except Exception as e:
        result["lan_error"] = str(e)

    # Enrich with hostnames from dnsmasq leases
    try:
        lease_paths = ["/var/lib/misc/dnsmasq.leases", "/var/lib/dnsmasq/dnsmasq.leases", "/tmp/dnsmasq.leases"]
        mac_to_host = {}
        for path in lease_paths:
            try:
                for line in Path(path).read_text().strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 4 and parts[3] != "*":
                        mac_to_host[parts[1].lower()] = parts[3]
                break
            except (FileNotFoundError, PermissionError):
                continue
        for c in result["ap_clients"] + result["lan_clients"]:
            c["hostname"] = mac_to_host.get(c.get("mac", "").lower(), "")
    except Exception:
        pass

    result["ap_count"] = len(result["ap_clients"])
    result["lan_count"] = len(result["lan_clients"])
    return jsonify(result)


# ── TCP Connection States (/proc/net/tcp) ────────────────────────────────────

@app.route("/api/network/tcp-states", methods=["GET"])
@require_auth
def api_network_tcp_states():
    """Return TCP connection state counts parsed from /proc/net/tcp and /proc/net/tcp6."""
    state_map = {
        "01": "ESTABLISHED",
        "02": "SYN_SENT",
        "03": "SYN_RECV",
        "04": "FIN_WAIT1",
        "05": "FIN_WAIT2",
        "06": "TIME_WAIT",
        "07": "CLOSE",
        "08": "CLOSE_WAIT",
        "09": "LAST_ACK",
        "0A": "LISTEN",
        "0B": "CLOSING",
    }
    states = {}
    listen_ports = set()
    total = 0
    ipv6_total = 0

    for path, is_v6 in [("/proc/net/tcp", False), ("/proc/net/tcp6", True)]:
        try:
            with open(path, "r") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines[1:]:  # skip header
            parts = line.split()
            if len(parts) < 4:
                continue
            local_raw = parts[1]
            state_hex = parts[3].upper()
            state_name = state_map.get(state_hex, state_hex)
            states[state_name] = states.get(state_name, 0) + 1
            total += 1
            if is_v6:
                ipv6_total += 1
            if state_name == "LISTEN":
                try:
                    port = int(local_raw.split(":")[1], 16)
                    listen_ports.add(port)
                except (IndexError, ValueError):
                    pass

    # Supplementary: ss for listening ports info
    try:
        import subprocess  # pylint: disable=import-outside-toplevel
        out = subprocess.run(
            ["ss", "-tuna"],
            capture_output=True, text=True, timeout=3
        ).stdout
        for line in out.splitlines()[1:50]:
            parts = line.split()
            if len(parts) >= 5 and parts[0] in ("LISTEN", "UNCONN"):
                addr = parts[4]
                try:
                    port = int(addr.rsplit(":", 1)[-1])
                    listen_ports.add(port)
                except ValueError:
                    pass
    except Exception:  # pylint: disable=broad-except
        pass

    return jsonify({
        "states": states,
        "total": total,
        "listen_ports": sorted(listen_ports),
        "ipv6_total": ipv6_total,
    })


# ── Boot Parameters ───────────────────────────────────────────────────────────

@app.route("/api/system/boot-params")
def api_boot_params():
    """Return kernel boot parameters, version, and key sysctl values."""
    try:
        def _read(path):
            try:
                with open(path) as f:
                    return f.read().strip()
            except Exception:
                return None

        def _read_int(path):
            v = _read(path)
            try:
                return int(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        cmdline = _read("/proc/cmdline") or ""
        params = []
        for token in cmdline.split():
            if "=" in token:
                k, _, v = token.partition("=")
                params.append({"key": k, "value": v})
            else:
                params.append({"key": token, "value": None})

        kernel_version = _read("/proc/version") or ""
        hostname = _read("/proc/sys/kernel/hostname") or ""
        pid_max = _read_int("/proc/sys/kernel/pid_max")
        threads_max = _read_int("/proc/sys/kernel/threads-max")
        swappiness = _read_int("/proc/sys/vm/swappiness")
        dirty_ratio = _read_int("/proc/sys/vm/dirty_ratio")

        boot_time = None
        try:
            import subprocess
            result = subprocess.run(
                ["systemd-analyze", "time"],
                capture_output=True, text=True, timeout=5
            )
            out = result.stdout.strip()
            # Parse lines like:
            # Startup finished in 1.234s (kernel) + 5.678s (userspace) = 6.912s
            total = kernel = userspace = None
            for line in out.splitlines():
                if "Startup finished" in line or "=" in line:
                    import re
                    m = re.search(r'=\s*([\d.]+\w+)', line)
                    if m:
                        total = m.group(1)
                    mk = re.search(r'([\d.]+\w+)\s*\(kernel\)', line)
                    if mk:
                        kernel = mk.group(1)
                    mu = re.search(r'([\d.]+\w+)\s*\(userspace\)', line)
                    if mu:
                        userspace = mu.group(1)
            if total or kernel or userspace:
                boot_time = {"total": total, "kernel": kernel, "userspace": userspace}
        except Exception:
            pass

        return jsonify({
            "cmdline": cmdline,
            "cmdline_params": params,
            "kernel_version": kernel_version,
            "hostname": hostname,
            "pid_max": pid_max,
            "threads_max": threads_max,
            "swappiness": swappiness,
            "dirty_ratio": dirty_ratio,
            "boot_time": boot_time,
            "error": None,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)})


_geoip_cache: dict = {}  # {"data": ..., "ts": float}
_GEOIP_TTL = 60  # seconds


@app.route("/api/network/geoip")
@require_auth
def api_network_geoip():
    """Return public IP and geolocation; cached for 60 s."""
    import time as _time
    import json as _json
    import subprocess as _sp

    now = _time.time()
    cached = _geoip_cache.get("data")
    if cached and (now - _geoip_cache.get("ts", 0)) < _GEOIP_TTL:
        cached["cached"] = True
        return jsonify(cached)

    def _fetch(url):
        try:
            out = _sp.check_output(
                ["curl", "-s", "--max-time", "5", url],
                stderr=_sp.DEVNULL,
                timeout=8,
            )
            return _json.loads(out.decode())
        except Exception:  # pylint: disable=broad-except
            return None

    # Primary: ipinfo.io
    data = _fetch("https://ipinfo.io/json")
    if data and data.get("ip") and "bogon" not in data:
        loc = data.get("loc", "")
        lat_s, _, lon_s = loc.partition(",")
        try:
            lat = float(lat_s) if lat_s else None
            lon = float(lon_s) if lon_s else None
        except ValueError:
            lat = lon = None
        result = {
            "ip": data.get("ip"),
            "hostname": data.get("hostname"),
            "city": data.get("city"),
            "region": data.get("region"),
            "country": data.get("country"),
            "country_name": None,
            "lat": lat,
            "lon": lon,
            "org": data.get("org"),
            "timezone": data.get("timezone"),
            "cached": False,
            "source": "ipinfo.io",
            "error": None,
        }
        _geoip_cache["data"] = result
        _geoip_cache["ts"] = now
        return jsonify(result)

    # Fallback: ip-api.com
    data2 = _fetch("https://ip-api.com/json")
    if data2 and data2.get("status") == "success":
        result = {
            "ip": data2.get("query"),
            "hostname": None,
            "city": data2.get("city"),
            "region": data2.get("regionName"),
            "country": data2.get("countryCode"),
            "country_name": data2.get("country"),
            "lat": data2.get("lat"),
            "lon": data2.get("lon"),
            "org": data2.get("org") or data2.get("isp"),
            "timezone": data2.get("timezone"),
            "cached": False,
            "source": "ip-api.com",
            "error": None,
        }
        _geoip_cache["data"] = result
        _geoip_cache["ts"] = now
        return jsonify(result)

    return jsonify({"error": "No internet access", "ip": None})


@app.route("/api/system/temperature")
@require_auth
def api_system_temperature():
    """Read thermal zone temperatures from /sys/class/thermal and vcgencmd."""
    zones = []
    thermal_base = Path("/sys/class/thermal")
    try:
        for zone_dir in sorted(thermal_base.glob("thermal_zone*")):
            try:
                temp_raw = (zone_dir / "temp").read_text().strip()
                zone_type = (zone_dir / "type").read_text().strip()
                temp_c = int(temp_raw) / 1000.0
                zones.append({"name": zone_type, "temp_c": round(temp_c, 1)})
            except (OSError, ValueError):
                continue
    except OSError:
        pass

    # Try vcgencmd for GPU temp
    gpu_out, gpu_rc = _run("vcgencmd measure_temp")
    if gpu_rc == 0 and gpu_out:
        import re as _re
        m = _re.search(r"temp=([\d.]+)'C", gpu_out)
        if m:
            zones.append({"name": "gpu", "temp_c": round(float(m.group(1)), 1)})

    max_c = max((z["temp_c"] for z in zones), default=0.0)
    return jsonify({
        "zones": zones,
        "max_c": round(max_c, 1),
        "warn": max_c >= 70.0,
        "critical": max_c >= 80.0,
    })


# ── Ping Latency ──────────────────────────────────────────────────────────────


@app.route("/api/network/latency")
@require_auth
def api_network_latency():
    """Ping a set of DNS targets plus the default gateway and return RTT stats."""
    static_targets = [
        {"host": "8.8.8.8", "label": "Google DNS"},
        {"host": "1.1.1.1", "label": "Cloudflare DNS"},
        {"host": "9.9.9.9", "label": "Quad9 DNS"},
    ]

    # Resolve default gateway
    gw_ip = None
    gw_out, gw_rc = _run("ip route show default")
    if gw_rc == 0 and gw_out.strip():
        import re as _re
        m = _re.search(r"via\s+(\S+)", gw_out)
        if m:
            gw_ip = m.group(1)

    targets = list(static_targets)
    if gw_ip:
        targets.append({"host": gw_ip, "label": "Gateway"})

    results = []
    for t in targets:
        host = t["host"]
        out, rc = _run(f"ping -c 3 -W 2 {host}", timeout=10)
        rtt_ms = None
        reachable = False
        if rc == 0 and out:
            import re as _re
            m = _re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/[\d.]+/[\d.]+", out)
            if m:
                try:
                    rtt_ms = float(m.group(1))
                    reachable = True
                except ValueError:
                    pass
        results.append({
            "host": host,
            "label": t["label"],
            "rtt_ms": rtt_ms,
            "reachable": reachable,
        })

    all_reachable = all(r["reachable"] for r in results)
    return jsonify({"targets": results, "all_reachable": all_reachable})


@app.route("/api/network/firewall", methods=["GET"])
@require_auth
def api_network_firewall():
    """Return iptables chain summary: policy, rule count, packet/byte stats."""
    import re as _re

    def _parse_chains(output):
        chains = []
        current_chain = None
        rule_count = 0
        for line in output.splitlines():
            # Chain header: "Chain INPUT (policy DROP 1024 packets, 98304 bytes)"
            m = _re.match(
                r"^Chain\s+(\S+)\s+\(policy\s+(\S+)\s+(\d+)\s+packets,\s+(\d+)\s+bytes\)",
                line,
            )
            if m:
                if current_chain is not None:
                    current_chain["rules"] = rule_count
                    chains.append(current_chain)
                current_chain = {
                    "name": m.group(1),
                    "policy": m.group(2),
                    "packets": int(m.group(3)),
                    "bytes": int(m.group(4)),
                    "rules": 0,
                }
                rule_count = 0
                continue
            # Also handle chains referenced by name without policy (e.g. user-defined)
            m2 = _re.match(r"^Chain\s+(\S+)\s+\((\d+)\s+references\)", line)
            if m2:
                if current_chain is not None:
                    current_chain["rules"] = rule_count
                    chains.append(current_chain)
                current_chain = {
                    "name": m2.group(1),
                    "policy": "—",
                    "packets": 0,
                    "bytes": 0,
                    "rules": 0,
                }
                rule_count = 0
                continue
            # Count rule rows (non-empty lines that aren't the header row)
            if current_chain is not None and line.strip() and not line.startswith("pkts"):
                rule_count += 1
        if current_chain is not None:
            current_chain["rules"] = rule_count
            chains.append(current_chain)
        return chains

    ipv4_out, _ = _run("iptables -L -n -v --line-numbers 2>/dev/null", timeout=10)
    ip6_out, ip6_rc = _run("ip6tables -L -n -v --line-numbers 2>/dev/null", timeout=10)

    chains = _parse_chains(ipv4_out)
    total_rules = sum(c["rules"] for c in chains)
    ipv6_available = ip6_rc == 0 and bool(ip6_out.strip())

    return jsonify({
        "chains": chains,
        "total_rules": total_rules,
        "ipv6_available": ipv6_available,
    })


@app.route("/api/system/clock", methods=["GET"])
@require_auth
def api_system_clock():
    import re as _re
    import datetime

    # Try chronyc tracking first
    out, rc = _run("chronyc tracking 2>/dev/null", timeout=5)
    if rc == 0 and out.strip():
        def _field(pattern, text, cast=str):
            m = _re.search(pattern, text)
            return cast(m.group(1)) if m else None

        ref_id = _field(r"Reference ID\s+:\s+(\S+)", out)
        # System time offset: e.g. "System time     :  0.000123456 seconds slow of NTP time"
        offset_s = _field(r"System time\s+:\s+([\-\d.]+)\s+seconds", out, float)
        rms_s = _field(r"RMS offset\s+:\s+([\-\d.]+)\s+seconds", out, float)
        freq_ppm = _field(r"Frequency\s+:\s+([\-\d.]+)\s+ppm", out, float)
        stratum = _field(r"Stratum\s+:\s+(\d+)", out, int)
        leap = _field(r"Leap status\s+:\s+(.+)", out)
        leap = leap.strip() if leap else None

        return jsonify({
            "synced": True,
            "source": "chrony",
            "stratum": stratum,
            "offset_ms": round(offset_s * 1000, 6) if offset_s is not None else None,
            "rms_offset_ms": round(rms_s * 1000, 6) if rms_s is not None else None,
            "freq_error_ppm": freq_ppm,
            "ref_id": ref_id,
            "leap_status": leap,
            "local_time": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    # Fallback: timedatectl show
    show_out, show_rc = _run("timedatectl show --no-pager 2>/dev/null", timeout=5)
    td_out, _ = _run("timedatectl 2>/dev/null", timeout=5)

    if show_rc == 0 and show_out.strip():
        def _kv(key, text):
            m = _re.search(r"^" + key + r"=(.+)$", text, _re.MULTILINE)
            return m.group(1).strip() if m else None

        ntp_sync = _kv("NTPSynchronized", show_out)
        synced = ntp_sync == "yes"
        # Parse NTPMessage for offset if available
        ntp_msg = _kv("NTPMessage", show_out) or ""
        offset_ms = None
        m_off = _re.search(r"offset=([\-\d.]+)", ntp_msg)
        if m_off:
            try:
                offset_ms = float(m_off.group(1)) * 1000
            except ValueError:
                pass

        return jsonify({
            "synced": synced,
            "source": "timedatectl",
            "stratum": None,
            "offset_ms": offset_ms,
            "rms_offset_ms": None,
            "freq_error_ppm": None,
            "ref_id": _kv("ServerName", show_out) or _kv("ServerAddress", show_out),
            "leap_status": None,
            "local_time": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    return jsonify({"synced": False, "source": None})


@app.route("/api/network/connections")
@require_auth
def api_network_connections():
    """Return established TCP/UDP connections with process names via ss."""
    import re as _re

    out, _ = _run("ss -tunp state established")
    connections = []
    by_proto: dict = {"tcp": 0, "udp": 0}

    for line in out.splitlines():
        # Skip header lines
        if line.startswith("Netid") or line.startswith("State"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = parts[0].lower()  # tcp / udp
        # columns: Netid State Recv-Q Send-Q Local Peer [process]
        # When "state established" filter is used, State column may be omitted
        # ss -tunp state established: Netid Recv-Q Send-Q Local Peer [users]
        # Detect layout by checking if parts[1] is a digit (Recv-Q)
        if parts[1].lstrip("-").isdigit():
            local = parts[3]
            peer = parts[4]
            process_col = parts[5] if len(parts) > 5 else ""
        else:
            # State column present
            local = parts[4]
            peer = parts[5]
            process_col = parts[6] if len(parts) > 6 else ""

        # Extract process name from users:(("name",pid=X,...))
        proc_match = _re.search(r'users:\(\("([^"]+)"', process_col)
        process = proc_match.group(1) if proc_match else ""

        connections.append({
            "proto": proto,
            "local": local,
            "remote": peer,
            "process": process,
        })
        if proto in by_proto:
            by_proto[proto] += 1
        else:
            by_proto[proto] = 1

    # Sort by remote IP, cap at top 20
    connections.sort(key=lambda c: c["remote"])
    connections = connections[:20]

    return jsonify({
        "connections": connections,
        "total": len(connections),
        "by_proto": by_proto,
    })


@app.route("/api/network/ap-clients", methods=["GET"])
@require_auth
def api_network_ap_clients():
    """Return list of stations connected to the AP (hostapd_cli / iw fallback)."""
    import re as _re
    iface = "wlan0"

    def _parse_hostapd(output):
        clients = []
        current = {}
        for line in output.splitlines():
            line = line.strip()
            if not line:
                if current.get("mac"):
                    clients.append(current)
                current = {}
                continue
            if _re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", line, _re.I):
                if current.get("mac"):
                    clients.append(current)
                current = {"mac": line.lower(), "signal_dbm": None, "rx_bytes": 0,
                           "tx_bytes": 0, "inactive_s": None}
            elif "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if key == "signal" and current:
                    m = _re.search(r"([-\d]+)", val)
                    if m:
                        current["signal_dbm"] = int(m.group(1))
                elif key == "rx_bytes" and current:
                    try:
                        current["rx_bytes"] = int(val)
                    except ValueError:
                        pass
                elif key == "tx_bytes" and current:
                    try:
                        current["tx_bytes"] = int(val)
                    except ValueError:
                        pass
                elif key == "inactive_msec" and current:
                    try:
                        current["inactive_s"] = round(int(val) / 1000.0, 1)
                    except ValueError:
                        pass
                elif key == "connected_time" and current:
                    try:
                        current["connected_time_s"] = int(val)
                    except ValueError:
                        pass
        if current.get("mac"):
            clients.append(current)
        return clients

    def _parse_iw(output):
        clients = []
        current = {}
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("Station"):
                if current.get("mac"):
                    clients.append(current)
                parts = line.split()
                current = {"mac": parts[1].lower() if len(parts) > 1 else "?",
                           "signal_dbm": None, "rx_bytes": 0, "tx_bytes": 0,
                           "inactive_s": None}
            elif current.get("mac"):
                m = _re.search(r"signal:\s*([-\d]+)", line)
                if m:
                    current["signal_dbm"] = int(m.group(1))
                m = _re.search(r"rx bytes:\s*(\d+)", line)
                if m:
                    current["rx_bytes"] = int(m.group(1))
                m = _re.search(r"tx bytes:\s*(\d+)", line)
                if m:
                    current["tx_bytes"] = int(m.group(1))
                m = _re.search(r"inactive time:\s*(\d+)\s*ms", line)
                if m:
                    current["inactive_s"] = round(int(m.group(1)) / 1000.0, 1)
                m = _re.search(r"connected time:\s*(\d+)\s*seconds", line)
                if m:
                    current["connected_time_s"] = int(m.group(1))
        if current.get("mac"):
            clients.append(current)
        return clients

    # Try hostapd_cli first
    out, rc = _run("hostapd_cli all_sta 2>/dev/null", timeout=5)
    if rc == 0 and out.strip():
        clients = _parse_hostapd(out)
    else:
        # Fallback: iw
        out, rc = _run(f"iw dev {iface} station dump 2>/dev/null", timeout=5)
        if rc != 0:
            # Try uap0 as well
            out, rc = _run("iw dev uap0 station dump 2>/dev/null", timeout=5)
            if rc == 0:
                iface = "uap0"
        clients = _parse_iw(out) if rc == 0 else []

    return jsonify({"clients": clients, "count": len(clients), "interface": iface})


# ── ARP / Neighbor Table ──────────────────────────────────────────────────────

@app.route("/api/network/arp", methods=["GET"])
@require_auth
def api_network_arp():
    """Return the kernel ARP/neighbor table parsed from /proc/net/arp."""
    _FLAG_MAP = {
        "0x0": "INCOMPLETE",
        "0x2": "REACHABLE",
        "0x4": "STALE",
        "0x6": "STALE",
    }
    entries = []
    try:
        with open("/proc/net/arp") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("IP address"):
                    continue
                parts = line.split()
                if len(parts) < 6:
                    continue
                ip, _hwtype, flags, mac, _mask, iface = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
                if mac == "00:00:00:00:00:00":
                    continue
                state = _FLAG_MAP.get(flags.lower(), "UNKNOWN")
                entries.append({"ip": ip, "mac": mac, "iface": iface, "flags": flags, "state": state})
    except OSError as exc:
        return jsonify({"entries": [], "count": 0, "error": str(exc)})
    return jsonify({"entries": entries, "count": len(entries)})


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


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=False)
