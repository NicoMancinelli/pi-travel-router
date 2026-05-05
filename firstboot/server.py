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
]


def _first(form: dict, key: str, default: str = "") -> str:
    v = form.get(key, [default])
    return v[0] if v else default


def _validate(form: dict) -> tuple[dict, list[str], str]:
    """Return (values, errors, new_root_password)."""
    errors: list[str] = []
    values: dict[str, str] = {}

    ap_ssid = _first(form, "AP_SSID", "TravelRouter").strip()
    if not (1 <= len(ap_ssid) <= 32):
        errors.append("AP SSID must be 1-32 characters.")
    values["AP_SSID"] = ap_ssid

    ap_pass = _first(form, "AP_PASS")
    if not (8 <= len(ap_pass) <= 63):
        errors.append("AP passphrase must be 8-63 characters.")
    values["AP_PASS"] = ap_pass

    country = _first(form, "COUNTRY", "US").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        errors.append("Country code must be two letters (e.g. US).")
    values["COUNTRY"] = country

    ntfy = _first(form, "NTFY_TOPIC").strip()
    if ntfy and not re.fullmatch(r"[A-Za-z0-9._-]+", ntfy):
        errors.append("ntfy.sh topic may only contain letters, numbers, dot, underscore, dash.")
    values["NTFY_TOPIC"] = ntfy

    ts_key = _first(form, "TS_KEY").strip()
    headscale_url = _first(form, "HEADSCALE_URL").strip()
    if ts_key and not headscale_url and not ts_key.startswith("tskey-auth-"):
        errors.append("Tailscale auth key must start with tskey-auth-.")
    values["TS_KEY"] = ts_key
    values["HEADSCALE_URL"] = headscale_url

    ssh_key = _first(form, "SSH_ADMIN_KEY").strip()
    if ssh_key and not re.match(r"^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-)", ssh_key):
        errors.append("SSH admin public key must be a valid OpenSSH public key.")
    values["SSH_ADMIN_KEY"] = ssh_key

    values["SPLIT_TUNNEL_DOMAINS"] = _first(form, "SPLIT_TUNNEL_DOMAINS").strip()

    hostname = _first(form, "ROUTER_HOSTNAME", "travelrouter").strip().lower()
    if hostname and not re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,62}", hostname):
        errors.append("Hostname must be 1-63 chars: letters, numbers, and hyphens only.")
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
    values["TOR_AP_PASS"] = tor_ap_pass

    vpn_device_macs = _first(form, "VPN_DEVICE_MACS").strip()
    if vpn_device_macs:
        mac_re = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
        for token in vpn_device_macs.split():
            if not mac_re.match(token):
                errors.append(f"Invalid MAC address in VPN device MACs: {token!r}")
                break
    values["VPN_DEVICE_MACS"] = vpn_device_macs

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
        f"bash install.sh > {shlex.quote(LOG_FILE)} 2>&1 && "
        f"{{ [[ -s {rootpw_q} ]] && chpasswd < {rootpw_q} && rm -f {rootpw_q} || true; }} && "
        f"touch {shlex.quote(DONE_FILE)} && "
        f"systemctl disable firstboot.service && "
        f"reboot || "
        f"{{ echo $? > {shlex.quote(FAIL_FILE)}; }}"
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
<form method="POST" action="/retry"><button type="submit">Retry setup</button></form>
<p class="warn">If the error repeats, SSH in as root and check /var/log/firstboot-install.log</p>
</div></body></html>
"""
    return body.encode("utf-8")


def parse_sections(text: str) -> list[str]:
    """Extract section names from install.sh log text. ANSI-stripped, in order."""
    cleaned = ANSI_RE.sub("", text)
    sections: list[str] = []
    for line in cleaned.splitlines():
        m = SECTION_RE.search(line)
        if m:
            sections.append(m.group(1).strip())
    return sections


def _read_log_text() -> str:
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):
            try:
                with open(INDEX_HTML, "rb") as fh:
                    body = fh.read()
            except FileNotFoundError:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b"index.html missing", "text/plain")
                return
            if _preseed:
                script = '<script>var _ps=' + json.dumps(_preseed) + ';'
                script += 'if(_ps.AP_SSID){var e=document.getElementById("ap_ssid");if(e)e.value=_ps.AP_SSID;}'
                script += 'if(_ps.SSH_ADMIN_KEY){var e=document.getElementById("sshkey");if(e)e.value=_ps.SSH_ADMIN_KEY;}'
                script += 'if(_ps.AP_PASS){var e=document.getElementById("ap_pass");if(e)e.value=_ps.AP_PASS;}'
                script += '</script>'
                body = body.replace(b'</body>', script.encode() + b'</body>')
            self._send(HTTPStatus.OK, body)
            return
        if self.path == "/status":
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
        if self.path == "/retry":
            for path in (ENV_FILE, FAIL_FILE, ROOTPW_FILE, LOG_FILE):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path != "/setup":
            self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 1_000_000:
            self._send(HTTPStatus.BAD_REQUEST, b"Bad request", "text/plain")
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw, keep_blank_values=True)
        values, errors, new_root_pw = _validate(form)
        if errors:
            self._send(HTTPStatus.BAD_REQUEST, _error_page(errors))
            return
        try:
            _write_env_file(values)
            _write_rootpw_file(new_root_pw)
        except OSError as exc:
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
        # SSH pubkey: look for lines with 'echo 'ssh-' or authorized_keys
        pubkey = None
        for line in content.splitlines():
            if "authorized_keys" in line or ("echo" in line and "ssh-" in line):
                m = re.search(r"(ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-\S+\s+\S+)", line)
                if m:
                    pubkey = m.group(1).strip()
                    break
        if pubkey:
            result["SSH_ADMIN_KEY"] = pubkey
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
            result["AP_SSID"] = hostname
        # AP_PASS: explicitly pre-seeded by the user in firstrun.sh
        m = re.search(r'\bAP_PASS=(["\']?)([^"\'\s]+)\1', content)
        if m:
            ap_pass_val = m.group(2)
            if len(ap_pass_val) >= 8:
                result["AP_PASS"] = ap_pass_val
        return result
    except Exception:
        return {}


def main() -> int:
    os.makedirs(STATE_DIR, exist_ok=True)
    addr = ("0.0.0.0", 80)
    httpd = HTTPServer(addr, Handler)
    sys.stderr.write(f"[firstboot] listening on {addr[0]}:{addr[1]}\n")
    _preseed.update(_load_preseed())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
