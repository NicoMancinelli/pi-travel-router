#!/usr/bin/env python3
"""First-boot setup wizard for pi-travel-router.

Listens on :80, serves a single-page HTML form, and on submit writes the user's
answers to /var/lib/travel-router/firstboot-env.sh, then spawns install.sh in
non-interactive mode. Stdlib only.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

STATE_DIR = "/var/lib/travel-router"
ENV_FILE = os.path.join(STATE_DIR, "firstboot-env.sh")
ROOTPW_FILE = os.path.join(STATE_DIR, "firstboot-rootpw")
DONE_FILE = os.path.join(STATE_DIR, "firstboot-done")
FAIL_FILE = os.path.join(STATE_DIR, "firstboot-failed")
LOG_FILE = "/var/log/firstboot-install.log"
REPO_DIR = "/opt/pi-travel-router"
INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Allowlisted Host header values (bare hostname or host:port, case-insensitive)
_ALLOWED_HOSTS = {
    "travelrouter.local",
    "192.168.7.1",
    "192.168.4.1",
    "10.3.141.1",
    "localhost",
    "127.0.0.1",
    "::1",
}
_BARE_IP_RE = re.compile(
    r"^(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?$"
    r"|^\[?[0-9a-fA-F:]+\]?(?::\d+)?$"
)
SECTION_RE = re.compile(r"━━\s*(.+?)\s*━━")

BOOL_FLAGS = [
    "ENABLE_VPN_KILLSWITCH",
    "ENABLE_BLOCKLISTS",
    "ENABLE_DOT",
    "ENABLE_AUTO_UPDATES",
    "ENABLE_AVAHI_REFLECTOR",
    "ENABLE_ADGUARD",
    "ENABLE_AP_SCHEDULE",
    "ENABLE_CLIENT_QOS",
    "ENABLE_CAKE_AUTOTUNE",
    "ENABLE_BANDWIDTH_DASHBOARD",
    "ENABLE_PROMETHEUS_EXPORTER",
    "ENABLE_UPS_MONITOR",
    "ENABLE_2FA",
    "ENABLE_SPLIT_TUNNEL",
    "ENABLE_WAN_METRICS",
    "ENABLE_TOR_TRANSPARENT",
    "ENABLE_HTTP_UA_REWRITE",
    "ENABLE_OPEN_WIFI_FALLBACK",
    "ENABLE_PER_DEVICE_VPN",
]

STRING_FIELDS = [
    "AP_SSID",
    "AP_PASS",
    "COUNTRY",
    "ROUTER_HOSTNAME",
    "ROUTER_TIMEZONE",
    "NTFY_TOPIC",
    "TS_KEY",
    "SSH_ADMIN_KEY",
    "HEADSCALE_URL",
    "SPLIT_TUNNEL_DOMAINS",
    "TOR_AP_PASS",
    "VPN_DEVICE_MACS",
    "IPHONE_BT_MAC",
    "AP_CLIENT_BANDWIDTH",
    "AP_DISABLE_TIME",
    "AP_ENABLE_TIME",
    "UPS_SHUTDOWN_THRESHOLD",
    "PUSHGW_URL",
    "TAILSCALE_UP_ARGS",
]

# Module-level flag to prevent double-submit
_installing = False

# CSRF token generated at startup
_csrf_token = secrets.token_hex(16)


def _log(msg: str) -> None:
    sys.stderr.write("[firstboot] " + msg.rstrip("\n") + "\n")


def _first(form: dict, key: str, default: str = "") -> str:
    v = form.get(key, [default])
    return v[0] if v else default


def _validate(form: dict) -> tuple[dict, list[str], str]:
    """Return (values, errors, new_root_password)."""
    errors: list[str] = []
    values: dict[str, str] = {}

    ap_ssid = _first(form, "AP_SSID", "TravelRouter").strip()
    if not (1 <= len(ap_ssid.encode("utf-8")) <= 32):
        errors.append("AP SSID must be 1–32 bytes (UTF-8 encoded).")
    if any(ord(c) < 0x20 for c in ap_ssid):
        errors.append("AP SSID may not contain control characters.")
    values["AP_SSID"] = ap_ssid

    ap_pass = _first(form, "AP_PASS")
    if not (8 <= len(ap_pass) <= 63):
        errors.append("AP passphrase must be 8-63 characters.")
    if ap_pass and not all(0x20 <= ord(c) <= 0x7E and c != '#' for c in ap_pass):
        errors.append("AP passphrase must use printable ASCII, excluding '#'.")
    values["AP_PASS"] = ap_pass

    country = _first(form, "COUNTRY", "US").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        errors.append("Country code must be two letters (e.g. US).")
    values["COUNTRY"] = country

    ntfy = _first(form, "NTFY_TOPIC").strip()
    if ntfy and not re.fullmatch(r"[A-Za-z0-9._-]+", ntfy):
        errors.append("ntfy.sh topic may only contain letters, numbers, dot, underscore, dash.")
    if len(ntfy) > 64:
        errors.append("ntfy.sh topic must be 64 characters or fewer")
    values["NTFY_TOPIC"] = ntfy

    ts_key = _first(form, "TS_KEY").strip()
    headscale_url = _first(form, "HEADSCALE_URL").strip()
    if ts_key and not headscale_url and not ts_key.startswith("tskey-auth-"):
        errors.append("Tailscale auth key must start with tskey-auth-.")
    if headscale_url:
        import urllib.parse
        parsed = urllib.parse.urlparse(headscale_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            errors.append("Headscale URL must be a valid http:// or https:// URL")
    values["TS_KEY"] = ts_key
    values["HEADSCALE_URL"] = headscale_url

    ssh_key = _first(form, "SSH_ADMIN_KEY").strip()
    if ssh_key and not re.match(r"^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-)", ssh_key):
        errors.append("SSH admin public key must be a valid OpenSSH public key.")
    values["SSH_ADMIN_KEY"] = ssh_key

    values["SPLIT_TUNNEL_DOMAINS"] = _first(form, "SPLIT_TUNNEL_DOMAINS").strip()

    hostname = _first(form, "ROUTER_HOSTNAME", "travelrouter").strip().lower()
    if hostname and not re.fullmatch(r"[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?", hostname):
        errors.append("Hostname must be 1-63 chars: letters, numbers, and hyphens only (no leading/trailing hyphens).")
    values["ROUTER_HOSTNAME"] = hostname or "travelrouter"

    timezone = _first(form, "ROUTER_TIMEZONE").strip()
    # Accept empty (keep UTC) or a non-empty string that looks like a TZ name
    if timezone and not re.fullmatch(r"[A-Za-z][A-Za-z0-9/_\-+]{1,49}", timezone):
        errors.append("Invalid timezone value.")
    values["ROUTER_TIMEZONE"] = timezone

    for flag in BOOL_FLAGS:
        values[flag] = "1" if _first(form, flag) else "0"

    if values["ENABLE_SPLIT_TUNNEL"] == "1" and not values["SPLIT_TUNNEL_DOMAINS"]:
        errors.append("Split tunnel enabled but no domains supplied.")

    tor_ap_pass = _first(form, "TOR_AP_PASS")
    if values["ENABLE_TOR_TRANSPARENT"] == "1" and len(tor_ap_pass) < 8:
        errors.append("Tor AP passphrase must be 8+ characters.")
    if tor_ap_pass and not all(0x20 <= ord(c) <= 0x7E and c != '#' for c in tor_ap_pass):
        errors.append("Tor AP passphrase must use printable ASCII, excluding '#'.")
    values["TOR_AP_PASS"] = tor_ap_pass

    vpn_device_macs = _first(form, "VPN_DEVICE_MACS").strip()
    if vpn_device_macs:
        mac_re = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
        for token in vpn_device_macs.split():
            if not mac_re.match(token):
                errors.append(f"Invalid MAC address in VPN device MACs: {token!r}")
                break
    values["VPN_DEVICE_MACS"] = vpn_device_macs

    iphone_bt_mac = _first(form, "IPHONE_BT_MAC").strip()
    if iphone_bt_mac and not re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", iphone_bt_mac):
        errors.append("iPhone Bluetooth MAC must be in AA:BB:CC:DD:EE:FF format.")
    values["IPHONE_BT_MAC"] = iphone_bt_mac

    ap_client_bandwidth = _first(form, "AP_CLIENT_BANDWIDTH", "unlimited").strip()
    if ap_client_bandwidth and not re.fullmatch(r"([0-9]+(k|m|g)?bit|unlimited)", ap_client_bandwidth, re.IGNORECASE):
        errors.append("Per-client bandwidth cap must be e.g. 50mbit, 10mbit, unlimited.")
    values["AP_CLIENT_BANDWIDTH"] = ap_client_bandwidth or "unlimited"

    ap_disable_time = _first(form, "AP_DISABLE_TIME", "02:00").strip()
    if ap_disable_time and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", ap_disable_time):
        errors.append("AP off time must be in HH:MM format (00:00-23:59).")
    values["AP_DISABLE_TIME"] = ap_disable_time or "02:00"

    ap_enable_time = _first(form, "AP_ENABLE_TIME", "07:00").strip()
    if ap_enable_time and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", ap_enable_time):
        errors.append("AP on time must be in HH:MM format (00:00-23:59).")
    values["AP_ENABLE_TIME"] = ap_enable_time or "07:00"

    ups_threshold = _first(form, "UPS_SHUTDOWN_THRESHOLD", "10").strip()
    if ups_threshold and not re.fullmatch(r"[0-9]{1,3}", ups_threshold):
        errors.append("UPS shutdown threshold must be a number (percent).")
    elif ups_threshold:
        ups_val = int(ups_threshold)
        if not (1 <= ups_val <= 99):
            errors.append("UPS shutdown threshold must be between 1 and 99.")
    values["UPS_SHUTDOWN_THRESHOLD"] = ups_threshold or "10"

    pushgw_url = _first(form, "PUSHGW_URL").strip()
    if pushgw_url:
        import urllib.parse as _up
        _parsed_pgw = _up.urlparse(pushgw_url)
        if _parsed_pgw.scheme not in ("http", "https") or not _parsed_pgw.netloc:
            errors.append("Prometheus push gateway URL must be a valid http:// or https:// URL.")
    values["PUSHGW_URL"] = pushgw_url

    tailscale_up_args = _first(form, "TAILSCALE_UP_ARGS").strip()
    if len(tailscale_up_args) > 512:
        errors.append("Tailscale up args must be 512 characters or fewer.")
    values["TAILSCALE_UP_ARGS"] = tailscale_up_args

    # New root password (optional). Don't strip — passwords may legitimately
    # contain leading/trailing spaces, though rare; use as-is.
    new_root_pw = _first(form, "new_root_password")
    new_root_pw_confirm = _first(form, "new_root_password_confirm")
    if new_root_pw:
        if len(new_root_pw) < 8:
            errors.append("New root password must be at least 8 characters.")
        if new_root_pw != new_root_pw_confirm:
            errors.append("Root password and confirmation do not match.")
        if "\n" in new_root_pw or "\r" in new_root_pw:
            errors.append("Root password may not contain newline characters.")

    return values, errors, new_root_pw


def _write_env_file(values: dict[str, str]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    lines = ["#!/bin/bash", "# Generated by firstboot wizard. Do not edit manually.", ""]
    for key in STRING_FIELDS + BOOL_FLAGS:
        if key in values:
            lines.append(f"export {key}={shlex.quote(values[key])}")
    lines.append("export INSTALL_NONINTERACTIVE='1'")
    lines.append("")
    tmp = ENV_FILE + ".tmp"
    old_umask = os.umask(0o077)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        os.chmod(tmp, 0o600)
        os.replace(tmp, ENV_FILE)
    finally:
        os.umask(old_umask)


def _write_rootpw_file(new_root_pw: str) -> None:
    """Write 'root:<password>' to ROOTPW_FILE with mode 0600. Empty input removes the file."""
    if not new_root_pw:
        try:
            os.remove(ROOTPW_FILE)
        except FileNotFoundError:
            pass
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = ROOTPW_FILE + ".tmp"
    old_umask = os.umask(0o077)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(f"root:{new_root_pw}\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, ROOTPW_FILE)
    finally:
        os.umask(old_umask)


def _spawn_install() -> None:
    rootpw_q = shlex.quote(ROOTPW_FILE)
    cmd = (
        f"source {shlex.quote(ENV_FILE)} && "
        f"cd {shlex.quote(REPO_DIR)} && "
        f"timeout 1800 bash install.sh > {shlex.quote(LOG_FILE)} 2>&1; "
        f"_rc=$?; "
        f"if [ $_rc -eq 0 ]; then "
        f"{{ [[ -s {rootpw_q} ]] && chpasswd < {rootpw_q} && rm -f {rootpw_q} || true; }} && "
        f"touch {shlex.quote(DONE_FILE)} && "
        f"systemctl disable firstboot.service && "
        f"reboot; "
        f"else echo $_rc > {shlex.quote(FAIL_FILE)}; fi"
    )
    subprocess.Popen(
        ["nohup", "bash", "-c", cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


_BASE_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:#0d1117;color:#c9d1d9;margin:0;padding:1rem;line-height:1.45}
.wrap{max-width:760px;margin:0 auto}
h1{color:#58a6ff;margin:.25rem 0 1rem;font-size:1.5rem}
h2{color:#c9d1d9;font-size:1.05rem;margin:1.25rem 0 .5rem}
p{margin:.5rem 0}
a{color:#58a6ff}
pre{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:1rem;overflow:auto;font-size:.8rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;line-height:1.4;white-space:pre-wrap;word-break:break-word}
.spin{display:inline-block;width:1em;height:1em;border:2px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:s 1s linear infinite;vertical-align:middle;margin-right:.5rem}
@keyframes s{to{transform:rotate(360deg)}}
.steps{list-style:none;padding:0;margin:.5rem 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9rem}
.steps li{padding:.2rem 0;color:#8b949e}
.steps li.done{color:#3fb950}
.steps li.current{color:#58a6ff;font-weight:600}
.success{background:#0f2a14;border:1px solid #238636;border-radius:8px;padding:1.25rem;color:#aff5b4}
.success .check{display:inline-block;width:2rem;height:2rem;line-height:2rem;text-align:center;border-radius:50%;background:#238636;color:#fff;font-weight:700;margin-right:.5rem;vertical-align:middle}
.warn{background:#341a1a;border:1px solid #f85149;border-radius:6px;padding:.65rem .85rem;color:#ffa198;font-size:.9rem;margin:1rem 0}
"""


def _running_page() -> bytes:
    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Travel Router — Setting up</title>
<meta http-equiv="refresh" content="5; url=/status">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap"><h1><span class="spin"></span>Setting up…</h1>
<p>Redirecting to the live status page in 5 seconds. Setup typically takes 5-10 minutes.</p>
<p>The Pi will reboot automatically when finished. Reconnect to your new SSID afterward.</p>
<p><a href="/status">Check status now &rarr;</a></p>
</div></body></html>
"""
    return body.encode("utf-8")


def _error_page(errors: list[str]) -> bytes:
    items = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Travel Router — Errors</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_BASE_CSS}
.errbox{{background:#341a1a;border:1px solid #f85149;border-radius:8px;padding:1rem;color:#ffa198}}
.errbox ul{{margin:.5rem 0 0 1.2rem;padding:0}}
</style></head>
<body><div class="wrap"><h1 style="color:#f85149">Configuration errors</h1>
<div class="errbox"><ul>{items}</ul></div>
<p><a href="/">&larr; Back to setup</a></p></div></body></html>
"""
    return body.encode("utf-8")


def _failed_page() -> bytes:
    log_text = _read_log_text() if os.path.exists(LOG_FILE) else ""
    cleaned = ANSI_RE.sub("", log_text)
    tail = "\n".join(cleaned.splitlines()[-50:]) if cleaned else "(no log output)"
    tail_html = html.escape(tail)
    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Travel Router — Installation failed</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap">
<h1 style="color:#f85149">Installation failed</h1>
<pre>{tail_html}</pre>
<form method="POST" action="/retry" enctype="application/x-www-form-urlencoded">
<input type="hidden" name="_csrf_token" value="{{{{CSRF_TOKEN}}}}">
<button type="submit">Retry setup</button></form>
<p class="warn">If the error repeats, SSH in as root and check /var/log/firstboot-install.log</p>
</div></body></html>
"""
    # Replace placeholder with actual token
    return body.replace("{{{{CSRF_TOKEN}}}}", _csrf_token).encode("utf-8")


def parse_sections(text: str) -> list[str]:
    """Extract section names from install.sh log text. ANSI-stripped, in order."""
    cleaned = ANSI_RE.sub("", text)
    sections: list[str] = []
    for line in cleaned.splitlines():
        m = SECTION_RE.search(line)
        if m:
            sections.append(m.group(1).strip())
    return sections


_LOG_MAX_BYTES = 512 * 1024  # 512 KB cap


def _read_log_text() -> str:
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, 2)  # seek to end
            size = fh.tell()
            if size > _LOG_MAX_BYTES:
                fh.seek(size - _LOG_MAX_BYTES)
                fh.readline()  # discard partial first line
            else:
                fh.seek(0)
            return fh.read()
    except FileNotFoundError:
        return ""


def _read_ap_ssid() -> str:
    try:
        with open(ENV_FILE, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"export\s+AP_SSID=(.+)$", line.strip())
                if m:
                    raw = m.group(1)
                    # strip surrounding single quotes added by shlex.quote
                    if raw.startswith("'") and raw.endswith("'"):
                        return raw[1:-1].replace("'\\''", "'")
                    return raw
    except FileNotFoundError:
        pass
    return ""


def _status_page() -> bytes:
    if os.path.exists(FAIL_FILE):
        return _failed_page()
    done = os.path.exists(DONE_FILE)
    log_text = _read_log_text()
    sections = parse_sections(log_text)

    if done:
        ssid = _read_ap_ssid() or "your new"
        body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Travel Router — Setup complete</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap">
<div class="success"><span class="check">&#10003;</span><strong>Setup complete.</strong>
<p style="margin:.75rem 0 0">The Pi is rebooting. Reconnect to your new SSID <strong>{html.escape(ssid)}</strong> to use the router.</p>
<p style="margin:.5rem 0 0;color:#8b949e">This page will not respond after the reboot.</p>
</div>
</div></body></html>
"""
        return body.encode("utf-8")

    # Build steps list
    if sections:
        current = sections[-1]
        done_steps = sections[:-1]
        steps_html_parts = []
        for s in done_steps:
            steps_html_parts.append(f'<li class="done">[X] {html.escape(s)}</li>')
        steps_html_parts.append(f'<li class="current">[&gt;] {html.escape(current)}</li>')
        steps_html = "<ul class=\"steps\">" + "".join(steps_html_parts) + "</ul>"
        current_label = html.escape(current)
    else:
        steps_html = '<ul class="steps"><li class="current">[&gt;] Starting…</li></ul>'
        current_label = "Starting…"

    cleaned_log = ANSI_RE.sub("", log_text)
    tail = "\n".join(cleaned_log.splitlines()[-30:]) if cleaned_log else "(install log not yet created)"
    tail_html = html.escape(tail)

    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Travel Router — Status</title>
<meta http-equiv="refresh" content="5; url=/status">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{_BASE_CSS}</style></head>
<body><div class="wrap">
<h1><span class="spin"></span>Installing…</h1>
<p>Currently: <strong>{current_label}</strong></p>
<h2>Progress</h2>
{steps_html}
<h2>Recent log output</h2>
<pre>{tail_html}</pre>
<p style="color:#8b949e;font-size:.85rem">This page refreshes every 5 seconds.</p>
</div></body></html>
"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "TravelRouterFirstBoot/1.0"

    def log_message(self, format, *args):  # noqa: A002
        sys.stderr.write("[firstboot] " + (format % args) + "\n")

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: dict, code: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self._send(code, body, "application/json")

    def _host_allowed(self) -> bool:
        """Return True if the Host header is in the allowlist or is a bare IP."""
        host = self.headers.get("Host", "").strip().lower()
        # Strip port suffix for comparison against the named hosts
        bare = host.split(":")[0] if ":" in host and not host.startswith("[") else host
        if bare in _ALLOWED_HOSTS or host in _ALLOWED_HOSTS:
            return True
        # Accept any bare IPv4/IPv6 address (router may have other IPs)
        return bool(_BARE_IP_RE.match(host))

    def do_GET(self):  # noqa: N802
        if not self._host_allowed():
            self._send(HTTPStatus.BAD_REQUEST, b"Invalid Host header", "text/plain")
            return
        if self.path == "/" or self.path.startswith("/?"):
            try:
                with open(INDEX_HTML, "rb") as fh:
                    body = fh.read()
            except FileNotFoundError:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b"index.html missing", "text/plain")
                return
            # Inject CSRF token
            body = body.replace(
                b"{{CSRF_TOKEN}}",
                _csrf_token.encode("ascii"),
            )
            if _preseed:
                script = '<script>var _ps=' + json.dumps(_preseed).replace("</", "<\\/") + ';'
                script += 'if(_ps.AP_SSID){var e=document.getElementById("ap_ssid");if(e)e.value=_ps.AP_SSID;}'
                script += 'if(_ps.ROUTER_HOSTNAME){var e=document.getElementById("hostname");if(e)e.value=_ps.ROUTER_HOSTNAME;}'
                script += 'if(_ps.SSH_ADMIN_KEY){var e=document.getElementById("sshkey");if(e)e.value=_ps.SSH_ADMIN_KEY;}'
                script += 'if(_ps.AP_PASS){var e=document.getElementById("ap_pass");if(e)e.value=_ps.AP_PASS;}'
                script += '</script>'
                body = body.replace(b'</body>', script.encode() + b'</body>')
            self._send(HTTPStatus.OK, body)
            return
        if self.path == "/status":
            # Redirect to setup form if no install has started yet
            if not _installing and not any(os.path.exists(p) for p in (LOG_FILE, DONE_FILE, FAIL_FILE)):
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self._send(HTTPStatus.OK, _status_page())
            return
        if self.path == "/setup":
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"Use POST.", "text/plain")
            return
        if self.path == "/retry":
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"Use POST.", "text/plain")
            return
        self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")

    def do_POST(self):  # noqa: N802
        global _installing
        if not self._host_allowed():
            self._send(HTTPStatus.BAD_REQUEST, b"Invalid Host header", "text/plain")
            return
        if self.path == "/retry":
            # CSRF check for /retry
            ct = self.headers.get("Content-Type", "")
            if ct.startswith("application/x-www-form-urlencoded"):
                length = int(self.headers.get("Content-Length", "0") or 0)
                if 0 < length <= 1_000_000:
                    body_bytes = self.rfile.read(length)
                    try:
                        raw = body_bytes.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        self._send(HTTPStatus.BAD_REQUEST, b"Invalid UTF-8", "text/plain")
                        return
                    form = parse_qs(raw, keep_blank_values=True)
                    submitted_token = _first(form, "_csrf_token")
                    if not secrets.compare_digest(submitted_token, _csrf_token):
                        self._send(HTTPStatus.FORBIDDEN, b"Invalid or missing CSRF token", "text/plain")
                        return
                else:
                    self._send(HTTPStatus.BAD_REQUEST, b"Bad request", "text/plain")
                    return
            else:
                self._send(HTTPStatus.BAD_REQUEST, b"Expected application/x-www-form-urlencoded", "text/plain")
                return
            _installing = False
            for path in (ENV_FILE, FAIL_FILE, ROOTPW_FILE, LOG_FILE, DONE_FILE):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path != "/setup":
            self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")
            return
        # L9: Content-Type validation
        ct = self.headers.get("Content-Type", "")
        if not ct.startswith("application/x-www-form-urlencoded"):
            self._send_json({"error": "Expected application/x-www-form-urlencoded"}, code=415)
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 1_000_000:
            self._send(HTTPStatus.BAD_REQUEST, b"Bad request", "text/plain")
            return
        body_bytes = self.rfile.read(length)
        # H23: strict UTF-8 decode
        try:
            raw = body_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            self._send_json({"error": "Invalid UTF-8 in request body"}, code=400)
            return
        form = parse_qs(raw, keep_blank_values=True)
        # L7: CSRF check
        submitted_token = _first(form, "_csrf_token")
        if not secrets.compare_digest(submitted_token, _csrf_token):
            self._send_json({"error": "Invalid or missing CSRF token"}, code=403)
            return
        values, errors, new_root_pw = _validate(form)
        if errors:
            # Re-serve index.html with validated values as preseed and errors injected
            try:
                with open(INDEX_HTML, "rb") as fh:
                    err_body = fh.read()
            except FileNotFoundError:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b"index.html missing", "text/plain")
                return
            err_body = err_body.replace(b"{{CSRF_TOKEN}}", _csrf_token.encode("ascii"))
            # Inject preseed of submitted values so the form retains state
            err_preseed = {k: v for k, v in values.items()}
            err_script = '<script>var _ps=' + json.dumps(err_preseed).replace("</", "<\\/") + ';'
            err_script += 'if(_ps.AP_SSID){var e=document.getElementById("ap_ssid");if(e)e.value=_ps.AP_SSID;}'
            err_script += 'if(_ps.ROUTER_HOSTNAME){var e=document.getElementById("hostname");if(e)e.value=_ps.ROUTER_HOSTNAME;}'
            err_script += 'if(_ps.SSH_ADMIN_KEY){var e=document.getElementById("sshkey");if(e)e.value=_ps.SSH_ADMIN_KEY;}'
            err_script += 'if(_ps.AP_PASS){var e=document.getElementById("ap_pass");if(e)e.value=_ps.AP_PASS;}'
            # Inject errors list so JS can display the banner
            err_items_json = json.dumps(errors).replace("</", "<\\/")
            err_script += 'var _errors=' + err_items_json + ';'
            err_script += ('if(_errors&&_errors.length){'
                           'var el=document.getElementById("errlist");'
                           'var es=document.getElementById("errsum");'
                           'if(el&&es){'
                           '_errors.forEach(function(m){var li=document.createElement("li");li.textContent=m;el.appendChild(li);});'
                           'es.classList.add("show");'
                           'window.scrollTo({top:0,behavior:"smooth"});'
                           '}'
                           '}')
            err_script += '</script>'
            err_body = err_body.replace(b'</body>', err_script.encode("utf-8") + b'</body>')
            self._send(HTTPStatus.BAD_REQUEST, err_body)
            return
        # C9: double-submit guard — redirect to status page instead of raw JSON
        if _installing:
            self.send_response(302)
            self.send_header("Location", "/status")
            self.end_headers()
            return
        _installing = True
        try:
            _write_env_file(values)
            _write_rootpw_file(new_root_pw)
        except OSError as exc:
            _installing = False
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Failed to write config: {exc}".encode("utf-8"),
                "text/plain",
            )
            return
        _spawn_install()
        self._send(HTTPStatus.OK, _running_page())


_preseed: dict[str, str] = {}


def _load_preseed() -> dict[str, str]:
    try:
        firstrun_path = None
        for candidate in ("/boot/firmware/firstrun.sh", "/boot/firstrun.sh"):
            if os.path.exists(candidate):
                firstrun_path = candidate
                break
        if firstrun_path is None:
            return {}
        with open(firstrun_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        result: dict[str, str] = {}
        # SSH pubkey: scan every line for an OpenSSH public-key token.
        # Imager encodes the key differently across versions:
        #   echo 'ssh-ed25519 AAAA...' >> authorized_keys
        #   SSHPUBKEY="ssh-ed25519 AAAA..."
        #   install ... <<< "ssh-rsa AAAA..."
        # Scanning every line (not just ones with "echo" or "authorized_keys")
        # is more robust across Imager versions.
        pubkey = None
        for line in content.splitlines():
            m = re.search(
                r"(ssh-(?:rsa|ed25519|dss|xmss)|ecdsa-sha2-[A-Za-z0-9]+)"
                r"\s+([A-Za-z0-9+/]+=*)"
                r"(\s+\S+)?",
                line,
            )
            if m:
                # Reconstruct key: type + blob + optional comment
                pubkey = m.group(1) + " " + m.group(2)
                if m.group(3):
                    pubkey += m.group(3)
                pubkey = pubkey.strip()
                break
        if pubkey:
            result["SSH_ADMIN_KEY"] = pubkey
            try:
                os.makedirs("/root/.ssh", mode=0o700, exist_ok=True)
                os.chmod("/root/.ssh", 0o700)
                ak_path = "/root/.ssh/authorized_keys"
                existing = ""
                if os.path.exists(ak_path):
                    # Read before checking — imager-compat.sh may have already
                    # written the key; avoid creating a duplicate line.
                    with open(ak_path, "r", encoding="utf-8", errors="replace") as f:
                        existing = f.read()
                if pubkey not in existing:
                    # Open with O_CREAT | O_APPEND and explicit 0o600 so the
                    # file is never created world-readable (open("a") is
                    # umask-dependent and can produce 0o644).
                    fd = os.open(ak_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    try:
                        os.write(fd, (pubkey + "\n").encode("utf-8"))
                    finally:
                        os.close(fd)
                os.chmod(ak_path, 0o600)
            except Exception as _e:
                import traceback
                _log(f"SSH key write error: {_e}\n{traceback.format_exc()}")
        # WiFi SSID: nmcli ... ssid "VALUE" or wpa SSID_VALUE style
        ssid = None
        m = re.search(r'nmcli.*\bssid\b\s+"([^"]+)"', content)
        if m:
            ssid = m.group(1)
        else:
            m = re.search(r'\bSSID=(\S+)', content)
            if m:
                ssid = m.group(1).strip('"\'')
        if ssid:
            result["AP_SSID"] = ssid
        # Hostname: raspi-config nonint do_hostname VALUE or echo VALUE > /etc/hostname
        hostname = None
        m = re.search(r'raspi-config\s+nonint\s+do_hostname\s+(\S+)', content)
        if m:
            hostname = m.group(1).strip('"\'')
        else:
            m = re.search(r'echo\s+(\S+)\s*>\s*/etc/hostname', content)
            if m:
                hostname = m.group(1).strip('"\'')
        if hostname:
            result["ROUTER_HOSTNAME"] = hostname
        # AP_PASS: explicitly pre-seeded by the user in firstrun.sh
        m = re.search(r'\bAP_PASS=(["\']?)([^"\'\s]+)\1', content)
        if m:
            ap_pass_val = m.group(2)
            if len(ap_pass_val) >= 8:
                result["AP_PASS"] = ap_pass_val
        return result
    except Exception as e:
        import traceback
        _log(f"preseed error: {e}\n{traceback.format_exc()}")
        return {}


def main() -> int:
    os.makedirs(STATE_DIR, exist_ok=True)
    addr = ("", 80)
    httpd = HTTPServer(addr, Handler)
    sys.stderr.write(f"[firstboot] listening on 0.0.0.0:{addr[1]}\n")
    _preseed.update(_load_preseed())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
