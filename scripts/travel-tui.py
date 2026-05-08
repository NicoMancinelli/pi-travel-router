#!/usr/bin/env python3
"""Pi Travel Router — Python/Textual interactive TUI dashboard.

Usage: sudo python3 /usr/local/sbin/travel-tui.py
       or simply: sudo travel-tui   (if wrapper script prefers Python)

Requires python3-textual (apt install python3-textual).
Falls back to travel-tui-legacy if Textual is not installed.
"""

import os
import re
import subprocess
import sys
import shlex

# ── Root check ─────────────────────────────────────────────────────────────────
if os.geteuid() != 0:
    print("Run as root: sudo travel-tui", file=sys.stderr)
    sys.exit(1)

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
    from textual.reactive import reactive
    from textual.screen import Screen, ModalScreen
    from textual.widgets import (
        Button,
        DataTable,
        Footer,
        Header,
        Input,
        Label,
        ListItem,
        ListView,
        Log,
        Markdown,
        Static,
    )
    from textual.timer import Timer
except ImportError:
    print(
        "python3-textual not installed — run: sudo apt install python3-textual\n"
        "Falling back to bash TUI if available.",
        file=sys.stderr,
    )
    legacy = "/usr/local/sbin/travel-tui-legacy"
    if os.path.exists(legacy):
        os.execv(legacy, [legacy] + sys.argv[1:])
    sys.exit(1)


# ── Config helpers ─────────────────────────────────────────────────────────────
DEFAULTS_FILE = "/etc/default/travel-router"
AP_IFACE = os.environ.get("AP_IFACE", "uap0")


def read_defaults() -> dict:
    """Parse /etc/default/travel-router into a dict (strips shell quoting)."""
    cfg: dict = {}
    try:
        with open(DEFAULTS_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                # strip single or double quotes
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                cfg[key] = val
    except OSError:
        pass
    return cfg


def write_default(key: str, val: str) -> str | None:
    """Rewrite a single key in /etc/default/travel-router. Returns error string or None."""
    try:
        with open(DEFAULTS_FILE) as fh:
            lines = fh.readlines()
        pattern = re.compile(r"^" + re.escape(key) + r"=")
        new_line = f"{key}={shlex.quote(val)}\n"
        replaced = False
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = new_line
                replaced = True
                break
        if not replaced:
            lines.append(new_line)
        tmp = DEFAULTS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            fh.writelines(lines)
        os.replace(tmp, DEFAULTS_FILE)
        return None
    except OSError as exc:
        return str(exc)


def write_hostapd(key: str, val: str) -> str | None:
    """Rewrite a single key in /etc/hostapd/hostapd.conf. Returns error string or None."""
    path = "/etc/hostapd/hostapd.conf"
    try:
        with open(path) as fh:
            lines = fh.readlines()
        pattern = re.compile(r"^" + re.escape(key) + r"=")
        new_line = f"{key}={val}\n"
        replaced = False
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = new_line
                replaced = True
                break
        if not replaced:
            lines.append(new_line)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.writelines(lines)
        os.replace(tmp, path)
        return None
    except OSError as exc:
        return str(exc)


def read_hostapd(key: str) -> str:
    try:
        with open("/etc/hostapd/hostapd.conf") as fh:
            for line in fh:
                if line.startswith(key + "="):
                    return line.partition("=")[2].strip()
    except OSError:
        pass
    return ""


def run(cmd: list, timeout: int = 5) -> tuple[int, str, str]:
    """Run a command safely; returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError) as exc:
        return 1, "", str(exc)


def run_shell(cmd: list, timeout: int = 10) -> tuple[int, str]:
    """Run a command and return (returncode, combined output)."""
    rc, out, err = run(cmd, timeout)
    return rc, (out + err).strip()


def svc_active(name: str) -> bool:
    rc, _, _ = run(["systemctl", "is-active", "--quiet", name])
    return rc == 0


# ── Status data model ─────────────────────────────────────────────────────────
def collect_status() -> dict:
    """Collect all dashboard status data. Called async every 5s."""
    cfg = read_defaults()

    # Uplink
    uplink = ""
    state_file = "/var/lib/travel-router/uplink.state"
    if os.path.exists(state_file):
        try:
            uplink = open(state_file).read().strip()
            if not re.match(r"^[a-zA-Z0-9_.\-]{1,15}$", uplink):
                uplink = ""
        except OSError:
            uplink = ""

    if not uplink:
        _, out, _ = run(["ip", "route", "get", "1.1.1.1"])
        m = re.search(r"\bdev\s+(\S+)", out)
        if m:
            uplink = m.group(1)

    utype_map = {
        "rndis0": "Android USB", "usb0": "Android USB",
        "bnep0": "BT PAN", "wlan0": "WiFi STA",
        "tailscale0": "Tailscale",
    }
    if uplink.startswith("enx"):
        utype = "iPhone USB"
    else:
        utype = utype_map.get(uplink, uplink or "none")

    _, src_out, _ = run(["ip", "route", "get", "1.1.1.1"])
    src_ip = ""
    m = re.search(r"\bsrc\s+(\S+)", src_out)
    if m:
        src_ip = m.group(1)

    signal = ""
    if uplink == "wlan0":
        _, sig_out, _ = run(["iw", "dev", "wlan0", "link"])
        m = re.search(r"signal:\s*(-\d+)", sig_out)
        if m:
            signal = m.group(1) + " dBm"

    captive_portal = os.path.exists("/tmp/captive-portal-active")

    # Tailscale
    headscale_url = cfg.get("HEADSCALE_URL", "")
    ts_label = "Headscale" if headscale_url else "Tailscale"
    _, ts_ip_out, _ = run(["tailscale", "ip", "-4"])
    ts_ip = ts_ip_out.strip().split("\n")[0] if ts_ip_out.strip() else ""
    ts_peers = 0
    _, ts_json, _ = run(["tailscale", "status", "--json"], timeout=5)
    for m2 in re.finditer(r'"Online"\s*:\s*(true|false)', ts_json):
        if m2.group(1) == "true":
            ts_peers += 1

    # Access Point
    ap_ssid = read_hostapd("ssid")
    _, sta_out, _ = run(["iw", "dev", AP_IFACE, "station", "dump"])
    ap_clients = sta_out.count("Station ")
    hostapd_active = svc_active("hostapd")

    # System stats
    temp = "?"
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            temp = f"{int(fh.read().strip()) // 1000}°C"
    except OSError:
        pass

    _, up_out, _ = run(["uptime", "-p"])
    uptime_str = up_out.strip().replace("up ", "") or "?"

    ram_str = "?"
    _, free_out, _ = run(["free", "-m"])
    for line in free_out.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 3:
                ram_str = f"{parts[2]}M/{parts[1]}M"
            break

    disk_str = "?"
    _, df_out, _ = run(["df", "-h", "/"])
    lines = df_out.strip().splitlines()
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 4:
            disk_str = f"{parts[2]}/{parts[1]}"

    return {
        "cfg": cfg,
        "uplink": uplink,
        "utype": utype,
        "src_ip": src_ip,
        "signal": signal,
        "captive_portal": captive_portal,
        "ts_label": ts_label,
        "ts_ip": ts_ip,
        "ts_peers": ts_peers,
        "ap_ssid": ap_ssid,
        "ap_clients": ap_clients,
        "hostapd_active": hostapd_active,
        "temp": temp,
        "uptime": uptime_str,
        "ram": ram_str,
        "disk": disk_str,
    }


# ── Shared CSS ─────────────────────────────────────────────────────────────────
APP_CSS = """
Screen {
    background: #0d1117;
}

#header-bar {
    background: #00aabb;
    color: #0d1117;
    height: 1;
    padding: 0 2;
}

.panel {
    border: solid #00aabb;
    margin: 0 1;
    padding: 0 1;
}

.panel-title {
    color: #00aabb;
    text-style: bold;
}

.status-label {
    color: #888888;
}

.status-value {
    color: #ffffff;
}

.online {
    color: #00cc66;
}

.offline {
    color: #cc3333;
}

.dimmed {
    color: #555555;
}

.warning {
    color: #ffaa00;
    text-style: bold;
}

.error-text {
    color: #cc3333;
}

.success-text {
    color: #00cc66;
}

Button {
    margin: 0 1;
}

Button.danger {
    background: #660000;
    border: tall #cc0000;
}

Button.action {
    background: #004466;
    border: tall #0088cc;
}

DataTable {
    height: auto;
    max-height: 20;
}

Input {
    margin: 1 0;
}

.modal-dialog {
    background: #1a1f2e;
    border: double #00aabb;
    padding: 1 2;
    width: 60;
    height: auto;
}

#nav-panel {
    height: 3;
    background: #111827;
    padding: 0 2;
}
"""


# ── Modal dialogs ─────────────────────────────────────────────────────────────
class MessageModal(ModalScreen):
    """Simple informational/error modal."""

    BINDINGS = [Binding("escape,enter,q", "dismiss", "Close")]

    def __init__(self, title: str, message: str, variant: str = "info") -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._variant = variant

    def compose(self) -> ComposeResult:
        color = "success-text" if self._variant == "success" else "error-text" if self._variant == "error" else "status-value"
        with Container(classes="modal-dialog"):
            yield Label(self._title, classes="panel-title")
            yield Static("")
            yield Label(self._message, classes=color)
            yield Static("")
            yield Button("OK [Enter]", id="ok-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


class ConfirmModal(ModalScreen):
    """Yes/No confirmation modal. Returns True on confirm."""

    BINDINGS = [Binding("escape,n", "cancel", "Cancel"), Binding("y", "confirm", "Confirm")]

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self._title = title
        self._message = message

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog"):
            yield Label(self._title, classes="panel-title")
            yield Static("")
            yield Label(self._message, classes="warning")
            yield Static("")
            with Horizontal():
                yield Button("Yes [Y]", id="yes-btn", variant="error")
                yield Button("No [N/Esc]", id="no-btn", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes-btn")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class InputModal(ModalScreen):
    """Single-field input modal. Returns string or None on cancel."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, prompt: str, current: str = "", password: bool = False) -> None:
        super().__init__()
        self._title = title
        self._prompt = prompt
        self._current = current
        self._password = password

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog"):
            yield Label(self._title, classes="panel-title")
            yield Label(self._prompt, classes="status-label")
            if self._current:
                masked = "(set)" if self._password else self._current
                yield Label(f"Current: {masked}", classes="dimmed")
            yield Input(
                placeholder="Enter value (blank = keep current)",
                password=self._password,
                id="inp",
            )
            with Horizontal():
                yield Button("Save [Enter]", id="save-btn", variant="primary")
                yield Button("Cancel [Esc]", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            inp = self.query_one("#inp", Input)
            self.dismiss(inp.value if inp.value else None)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value if event.value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Dashboard screen ──────────────────────────────────────────────────────────
class DashboardScreen(Screen):
    """Main status overview with auto-refresh."""

    BINDINGS = [
        Binding("1", "push_screen('services')", "Services"),
        Binding("2", "push_screen('features')", "Features"),
        Binding("3", "push_screen('logs')", "Logs"),
        Binding("4", "push_screen('clients')", "Clients"),
        Binding("5", "push_screen('network')", "Network"),
        Binding("6", "push_screen('settings')", "Settings"),
        Binding("7", "push_screen('system')", "System"),
        Binding("w", "push_screen('wireguard')", "WireGuard"),
        Binding("r", "push_screen('routes')", "Routes"),
        Binding("q", "quit_app", "Quit"),
    ]

    _status: reactive[dict] = reactive({}, layout=True)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with ScrollableContainer():
            yield Static(id="uplink-panel", classes="panel")
            yield Static(id="ts-panel", classes="panel")
            yield Static(id="ap-panel", classes="panel")
            yield Static(id="features-panel", classes="panel")
            yield Static(id="system-panel", classes="panel")
        yield Static(
            "  [1]Services  [2]Features  [3]Logs  [4]Clients  [5]Network  "
            "[6]Settings  [7]System  [W]WireGuard  [R]Routes  [Q]Quit",
            id="nav-panel",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_timer: Timer = self.set_interval(5, self._refresh_status)
        self._refresh_status()

    def _refresh_status(self) -> None:
        self.run_worker(self._load_status, exclusive=True, thread=True)

    def _load_status(self) -> None:
        data = collect_status()
        self.call_from_thread(self._apply_status, data)

    def _apply_status(self, data: dict) -> None:
        self._status = data
        self._render_panels(data)

    def _render_panels(self, d: dict) -> None:
        cfg = d.get("cfg", {})

        # Uplink panel
        uplink = d.get("uplink", "none")
        utype = d.get("utype", "none")
        src_ip = d.get("src_ip", "?")
        signal = d.get("signal", "")
        cp = d.get("captive_portal", False)
        utype_disp = utype
        if signal:
            utype_disp += f" · {signal}"
        up_dot = "[@green]●[/]" if uplink else "[@red]○[/]"
        cp_warn = "  [@yellow bold]⚠ CAPTIVE PORTAL[/]" if cp else ""
        uplink_text = (
            f"[@cyan bold]UPLINK[/]\n"
            f"  {up_dot} {utype_disp}  [{uplink or 'none'}]  src {src_ip}{cp_warn}"
        )
        try:
            self.query_one("#uplink-panel", Static).update(uplink_text)
        except Exception:
            pass

        # Tailscale panel
        ts_label = d.get("ts_label", "Tailscale")
        ts_ip = d.get("ts_ip", "")
        ts_peers = d.get("ts_peers", 0)
        ts_dot = "[@green]●[/]" if ts_ip else "[@dim]○[/]"
        ts_text = (
            f"[@cyan bold]{ts_label}[/]\n"
            f"  {ts_dot} {ts_ip or 'not connected'}  [@dim]{ts_peers} peers online[/]"
        )
        try:
            self.query_one("#ts-panel", Static).update(ts_text)
        except Exception:
            pass

        # AP panel
        ap_ssid = d.get("ap_ssid", "?")
        ap_clients = d.get("ap_clients", 0)
        ha = d.get("hostapd_active", False)
        ap_dot = "[@green]●[/]" if ha else "[@red]○[/]"
        client_word = "client" if ap_clients == 1 else "clients"
        ap_text = (
            f"[@cyan bold]ACCESS POINT[/]\n"
            f"  {ap_dot} [@green]{ap_ssid}[/]  [@dim]{ap_clients} {client_word}[/]"
        )
        try:
            self.query_one("#ap-panel", Static).update(ap_text)
        except Exception:
            pass

        # Features panel
        def dot(flag: str) -> str:
            return "[@green]●[/]" if cfg.get(flag, "0") == "1" else "[@dim]○[/]"

        feat_text = (
            f"[@cyan bold]FEATURES[/]\n"
            f"  DoT {dot('ENABLE_DOT')}  "
            f"Kill {dot('ENABLE_VPN_KILLSWITCH')}  "
            f"Tor {dot('ENABLE_TOR_TRANSPARENT')}  "
            f"Blocks {dot('ENABLE_BLOCKLISTS')}  "
            f"AdGuard {dot('ENABLE_ADGUARD')}  "
            f"2FA {dot('ENABLE_2FA')}  "
            f"QoS {dot('ENABLE_CLIENT_QOS')}  "
            f"WG {dot('ENABLE_WIREGUARD')}"
        )
        try:
            self.query_one("#features-panel", Static).update(feat_text)
        except Exception:
            pass

        # System panel
        sys_text = (
            f"[@cyan bold]SYSTEM[/]\n"
            f"  [@dim]Temp[/] {d.get('temp','?')}  "
            f"[@dim]RAM[/] {d.get('ram','?')}  "
            f"[@dim]Disk[/] {d.get('disk','?')}  "
            f"[@dim]Up[/] {d.get('uptime','?')}"
        )
        try:
            self.query_one("#system-panel", Static).update(sys_text)
        except Exception:
            pass

    def action_push_screen(self, name: str) -> None:
        self.app.push_screen(name)

    def action_quit_app(self) -> None:
        self.app.exit()


# ── Services screen ───────────────────────────────────────────────────────────
SERVICES = [
    "tailscaled", "hostapd", "dnsmasq", "stubby", "adguard-home",
    "tor", "privoxy", "failover-watchdog", "wan-watchdog", "tailscale-watchdog",
]


class ServicesScreen(Screen):
    BINDINGS = [Binding("q,escape", "pop_screen", "Back"), Binding("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Services  —  select to restart", classes="panel-title")
        yield DataTable(id="svc-table", zebra_stripes=True)
        yield Static(id="svc-msg")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#svc-table", DataTable)
        t.add_columns("Service", "Status")
        self._load_services()

    def _load_services(self) -> None:
        self.run_worker(self._fetch_services, exclusive=True, thread=True)

    def _fetch_services(self) -> None:
        rc, out, _ = run(["systemctl", "is-active"] + SERVICES, timeout=8)
        states = out.strip().splitlines()
        rows = []
        for i, svc in enumerate(SERVICES):
            state = states[i] if i < len(states) else "unknown"
            icon = "● active" if state == "active" else "○ inactive"
            rows.append((svc, icon, state))
        self.call_from_thread(self._apply_services, rows)

    def _apply_services(self, rows: list) -> None:
        t = self.query_one("#svc-table", DataTable)
        t.clear()
        for svc, icon, state in rows:
            style = "green" if state == "active" else "red"
            t.add_row(svc, f"[{style}]{icon}[/{style}]")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(SERVICES):
            svc = SERVICES[idx]
            self.app.push_screen(
                ConfirmModal("Restart Service", f"Restart {svc}?"),
                lambda result, s=svc: self._do_restart(s) if result else None,
            )

    def _do_restart(self, svc: str) -> None:
        self.run_worker(lambda s=svc: self._restart_worker(s), thread=True)

    def _restart_worker(self, svc: str) -> None:
        rc, out, err = run(["systemctl", "restart", svc], timeout=15)
        msg = f"✓ {svc} restarted" if rc == 0 else f"✗ restart failed: {err[:80]}"
        variant = "success" if rc == 0 else "error"
        self.call_from_thread(
            lambda m=msg, v=variant: self.app.push_screen(MessageModal("Service", m, v))
        )
        self.call_from_thread(self._load_services)

    def action_refresh(self) -> None:
        self._load_services()


# ── Features screen ───────────────────────────────────────────────────────────
FEATURE_FLAGS = [
    "ENABLE_DOT",
    "ENABLE_VPN_KILLSWITCH",
    "ENABLE_AUTO_UPDATES",
    "ENABLE_AVAHI_REFLECTOR",
    "ENABLE_ADGUARD",
    "ENABLE_BLOCKLISTS",
    "ENABLE_TOR_TRANSPARENT",
    "ENABLE_HTTP_UA_REWRITE",
    "ENABLE_OPEN_WIFI_FALLBACK",
    "ENABLE_AP_SCHEDULE",
    "ENABLE_CLIENT_QOS",
    "ENABLE_PER_DEVICE_VPN",
    "ENABLE_CAKE_AUTOTUNE",
    "ENABLE_UPS_MONITOR",
    "ENABLE_BANDWIDTH_DASHBOARD",
    "ENABLE_SPLIT_TUNNEL",
    "ENABLE_2FA",
    "ENABLE_WAN_METRICS",
    "ENABLE_PROMETHEUS_EXPORTER",
    "ENABLE_WIREGUARD",
]


class FeaturesScreen(Screen):
    BINDINGS = [Binding("q,escape", "pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Feature Flags  —  select to toggle", classes="panel-title")
        yield DataTable(id="feat-table", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#feat-table", DataTable)
        t.add_columns("Flag", "State")
        self._load_flags()

    def _load_flags(self) -> None:
        cfg = read_defaults()
        t = self.query_one("#feat-table", DataTable)
        t.clear()
        for flag in FEATURE_FLAGS:
            val = cfg.get(flag, "0")
            icon = "[green]● on[/green]" if val == "1" else "[dim]○ off[/dim]"
            t.add_row(flag, icon)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(FEATURE_FLAGS):
            flag = FEATURE_FLAGS[idx]
            cfg = read_defaults()
            cur = cfg.get(flag, "0")
            new_val = "0" if cur == "1" else "1"
            self.run_worker(
                lambda f=flag, v=new_val: self._toggle_worker(f, v),
                thread=True,
            )

    def _toggle_worker(self, flag: str, new_val: str) -> None:
        err = write_default(flag, new_val)
        if err:
            self.call_from_thread(
                lambda e=err: self.app.push_screen(MessageModal("Error", e, "error"))
            )
            return
        # Side-effects
        self._apply_side_effects(flag, new_val)
        self.call_from_thread(self._load_flags)

    def _apply_side_effects(self, flag: str, new_val: str) -> None:
        enable = new_val == "1"
        try:
            if flag in ("ENABLE_VPN_KILLSWITCH", "ENABLE_TOR_TRANSPARENT",
                        "ENABLE_BLOCKLISTS", "ENABLE_PER_DEVICE_VPN"):
                run(["/usr/local/bin/travel-router-firewall.sh", "--save"], timeout=15)
                if flag == "ENABLE_BLOCKLISTS" and enable:
                    run(["systemctl", "start", "update-blocklists.service"])
            elif flag == "ENABLE_DOT":
                if enable:
                    run(["systemctl", "restart", "stubby"])
                else:
                    run(["systemctl", "stop", "stubby"])
                run(["systemctl", "reload-or-restart", "dnsmasq"])
            elif flag == "ENABLE_ADGUARD":
                if enable:
                    run(["systemctl", "restart", "adguard-home"])
                else:
                    run(["systemctl", "stop", "adguard-home"])
                run(["systemctl", "reload-or-restart", "dnsmasq"])
            elif flag == "ENABLE_AVAHI_REFLECTOR":
                run(["systemctl", "reload-or-restart", "avahi-daemon"])
            elif flag == "ENABLE_HTTP_UA_REWRITE":
                if enable:
                    run(["systemctl", "restart", "privoxy"])
                else:
                    run(["systemctl", "stop", "privoxy"])
            elif flag == "ENABLE_AP_SCHEDULE":
                if enable:
                    run(["systemctl", "enable", "--now", "ap-disable.timer", "ap-enable.timer"])
                else:
                    run(["systemctl", "disable", "--now", "ap-disable.timer", "ap-enable.timer"])
            elif flag == "ENABLE_CAKE_AUTOTUNE":
                if enable:
                    run(["systemctl", "enable", "--now", "tune-cake.timer"])
                else:
                    run(["systemctl", "disable", "--now", "tune-cake.timer"])
            elif flag == "ENABLE_CLIENT_QOS":
                run(["/usr/local/bin/apply-cake.sh"])
            elif flag == "ENABLE_AUTO_UPDATES":
                if enable:
                    run(["systemctl", "enable", "unattended-upgrades"])
                else:
                    run(["systemctl", "disable", "unattended-upgrades"])
            elif flag == "ENABLE_UPS_MONITOR":
                if enable:
                    run(["systemctl", "enable", "--now", "ups-monitor.timer"])
                else:
                    run(["systemctl", "disable", "--now", "ups-monitor.timer"])
            elif flag == "ENABLE_WAN_METRICS":
                if enable:
                    run(["systemctl", "enable", "--now", "wan-metrics.timer"])
                else:
                    run(["systemctl", "disable", "--now", "wan-metrics.timer"])
            elif flag == "ENABLE_PROMETHEUS_EXPORTER":
                if enable:
                    run(["systemctl", "enable", "--now", "prometheus-node-exporter"])
                else:
                    run(["systemctl", "disable", "--now", "prometheus-node-exporter"])
            elif flag == "ENABLE_SPLIT_TUNNEL":
                if enable:
                    run(["systemctl", "try-restart", "split-tunnel.service"])
                else:
                    run(["systemctl", "stop", "split-tunnel.service"])
            elif flag == "ENABLE_BANDWIDTH_DASHBOARD":
                if enable:
                    run(["systemctl", "try-restart", "bandwidth-dashboard.service"])
                else:
                    run(["systemctl", "stop", "bandwidth-dashboard.service"])
        except Exception:
            pass


# ── Logs screen ───────────────────────────────────────────────────────────────
LOG_SOURCES = [
    ("WAN watchdog (last 30)", "file", "/var/log/wan-watchdog.log"),
    ("Tailscale journal (last 30)", "journal", "tailscaled"),
    ("Failover watchdog (last 30)", "journal", "failover-watchdog"),
    ("hostapd journal (last 30)", "journal", "hostapd"),
    ("Failed systemd units", "failed", ""),
    ("update-router.log (last 20)", "file", "/var/log/update-router.log"),
    ("Combined log (live tail, 30 lines)", "file", "/var/log/travel-router/combined.log"),
]


class LogsScreen(Screen):
    BINDINGS = [Binding("q,escape", "pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Logs  —  select a source", classes="panel-title")
        table = DataTable(id="log-src-table", zebra_stripes=True)
        yield table
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#log-src-table", DataTable)
        t.add_column("Log Source")
        for label, _, _ in LOG_SOURCES:
            t.add_row(label)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(LOG_SOURCES):
            label, src_type, src = LOG_SOURCES[idx]
            self.app.push_screen(LogViewScreen(label, src_type, src))


class LogViewScreen(Screen):
    BINDINGS = [
        Binding("q,escape", "pop_screen", "Back"),
        Binding("r", "refresh_log", "Refresh"),
    ]

    def __init__(self, title: str, src_type: str, src: str) -> None:
        super().__init__()
        self._title = title
        self._src_type = src_type
        self._src = src
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label(self._title, classes="panel-title")
        yield Log(id="log-view", auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self._load_log()
        # Auto-refresh combined log every 2s
        if "combined.log" in self._src:
            self._timer = self.set_interval(2, self._load_log)

    def on_unmount(self) -> None:
        if self._timer:
            self._timer.stop()

    def _load_log(self) -> None:
        self.run_worker(self._fetch_log, exclusive=True, thread=True)

    def _fetch_log(self) -> None:
        lines = []
        try:
            if self._src_type == "file":
                with open(self._src) as fh:
                    all_lines = fh.readlines()
                n = 20 if "update-router" in self._src else 30
                lines = all_lines[-n:]
            elif self._src_type == "journal":
                _, out, _ = run(
                    ["journalctl", "-u", self._src, "-n", "30", "--no-pager"],
                    timeout=8,
                )
                lines = out.splitlines(keepends=True)
            elif self._src_type == "failed":
                _, out, _ = run(["systemctl", "--failed", "--no-pager"], timeout=8)
                lines = out.splitlines(keepends=True)
        except OSError as exc:
            lines = [f"(error reading log: {exc})\n"]

        text = "".join(lines) if lines else "(no entries)\n"
        self.call_from_thread(self._apply_log, text)

    def _apply_log(self, text: str) -> None:
        log_widget = self.query_one("#log-view", Log)
        log_widget.clear()
        log_widget.write(text)

    def action_refresh_log(self) -> None:
        self._load_log()


# ── Clients screen ────────────────────────────────────────────────────────────
class ClientsScreen(Screen):
    BINDINGS = [Binding("q,escape", "pop_screen", "Back"), Binding("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("AP Clients", classes="panel-title")
        yield DataTable(id="clients-table", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#clients-table", DataTable)
        t.add_columns("MAC", "IP", "Hostname", "Signal")
        self._load_clients()

    def _load_clients(self) -> None:
        self.run_worker(self._fetch_clients, exclusive=True, thread=True)

    def _fetch_clients(self) -> None:
        _, sta_out, _ = run(["iw", "dev", AP_IFACE, "station", "dump"])
        _, neigh_out, _ = run(["ip", "neigh", "show"])

        # Build MAC→IP map from ip neigh
        mac_to_ip: dict = {}
        for line in neigh_out.splitlines():
            parts = line.split()
            # format: IP dev IFACE lladdr MAC state STATE
            if "lladdr" in parts:
                ll_idx = parts.index("lladdr")
                ip_addr = parts[0]
                mac_addr = parts[ll_idx + 1].lower()
                mac_to_ip[mac_addr] = ip_addr

        # Also check /proc/net/arp
        try:
            with open("/proc/net/arp") as fh:
                for line in fh.readlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 4:
                        ip_addr = parts[0]
                        mac_addr = parts[3].lower()
                        if mac_addr not in mac_to_ip:
                            mac_to_ip[mac_addr] = ip_addr
        except OSError:
            pass

        # Parse station dump
        rows = []
        current_mac = None
        current_signal = "?"
        for line in sta_out.splitlines():
            if line.startswith("Station "):
                if current_mac:
                    ip = mac_to_ip.get(current_mac.lower(), "unknown")
                    hostname = self._lookup_hostname(ip)
                    rows.append((current_mac, ip, hostname, current_signal))
                current_mac = line.split()[1]
                current_signal = "?"
            elif "signal:" in line:
                parts = line.strip().split()
                if len(parts) >= 2:
                    current_signal = " ".join(parts[1:3])

        if current_mac:
            ip = mac_to_ip.get(current_mac.lower(), "unknown")
            hostname = self._lookup_hostname(ip)
            rows.append((current_mac, ip, hostname, current_signal))

        self.call_from_thread(self._apply_clients, rows)

    def _lookup_hostname(self, ip: str) -> str:
        if ip in ("unknown", ""):
            return ""
        try:
            with open("/var/lib/misc/dnsmasq.leases") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 4 and parts[2] == ip and parts[3] != "*":
                        return parts[3]
        except OSError:
            pass
        return ""

    def _apply_clients(self, rows: list) -> None:
        t = self.query_one("#clients-table", DataTable)
        t.clear()
        if not rows:
            t.add_row("No clients connected", "", "", "")
        else:
            for mac, ip, hostname, signal in rows:
                t.add_row(
                    f"[green]{mac}[/green]",
                    f"[dim]{ip}[/dim]",
                    hostname,
                    signal,
                )

    def action_refresh(self) -> None:
        self._load_clients()


# ── Network screen ────────────────────────────────────────────────────────────
class NetworkScreen(Screen):
    BINDINGS = [Binding("q,escape", "pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Network Tools", classes="panel-title")
        with ScrollableContainer():
            yield Button("Show WiFi QR Code", id="qr-btn", classes="action")
            yield Button("Clone MAC to wlan0", id="clone-mac-btn", classes="action")
            yield Button("Restore original wlan0 MAC", id="restore-mac-btn", classes="action")
            yield Button("Connect to hotel / new WiFi", id="wifi-connect-btn", classes="action")
            yield Button("Start Bluetooth tethering", id="bt-start-btn", classes="action")
            yield Button("Stop Bluetooth tethering", id="bt-stop-btn", classes="action")
            yield Button("Re-check captive portal", id="cp-check-btn", classes="action")
            yield Button("Run speedtest + update CAKE", id="speedtest-btn", classes="action")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "qr-btn":
            self._show_qr()
        elif bid == "clone-mac-btn":
            self.app.push_screen(
                InputModal("Clone MAC", "Enter MAC address (aa:bb:cc:dd:ee:ff):"),
                self._do_clone_mac,
            )
        elif bid == "restore-mac-btn":
            self.run_worker(lambda: self._run_cmd(
                ["/usr/local/bin/clone-mac.sh", "--restore"],
                "MAC restore"
            ), thread=True)
        elif bid == "wifi-connect-btn":
            self.app.push_screen(
                InputModal("WiFi Connect", "SSID to connect to:"),
                self._wifi_ssid_entered,
            )
        elif bid == "bt-start-btn":
            cfg = read_defaults()
            mac = cfg.get("IPHONE_BT_MAC", "")
            if not mac:
                self.app.push_screen(MessageModal("BT Tether", "IPHONE_BT_MAC not set in Settings", "error"))
            else:
                self.run_worker(lambda m=mac: self._run_cmd(
                    ["/usr/local/bin/start-bt-tether.sh", m],
                    "BT tether start"
                ), thread=True)
        elif bid == "bt-stop-btn":
            self.run_worker(lambda: self._run_cmd(
                ["/usr/local/bin/stop-bt-tether.sh"],
                "BT tether stop"
            ), thread=True)
        elif bid == "cp-check-btn":
            self.run_worker(self._check_captive, thread=True)
        elif bid == "speedtest-btn":
            self.run_worker(lambda: self._run_cmd(
                ["/usr/local/bin/tune-cake.sh"],
                "Speedtest", timeout=120
            ), thread=True)

    def _show_qr(self) -> None:
        ssid = read_hostapd("ssid")
        passwd = read_hostapd("wpa_passphrase")
        auth = "WPA" if passwd else "nopass"
        # MECARD escaping
        for ch in ("\\", ";", ",", '"', ":"):
            ssid = ssid.replace(ch, "\\" + ch)
            passwd = passwd.replace(ch, "\\" + ch)
        wifi_str = f"WIFI:T:{auth};S:{ssid};P:{passwd};;"
        rc, out, err = run(["qrencode", "-t", "ansiutf8", wifi_str], timeout=5)
        if rc == 0:
            self.app.push_screen(MessageModal("WiFi QR Code", out or "(empty output)"))
        else:
            self.app.push_screen(MessageModal("WiFi QR", f"qrencode failed: {err[:80]}", "error"))

    def _do_clone_mac(self, mac: str | None) -> None:
        if not mac:
            return
        if not re.match(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$", mac):
            self.app.push_screen(MessageModal("Clone MAC", "Invalid MAC format", "error"))
            return
        self.run_worker(lambda m=mac: self._run_cmd(
            ["/usr/local/bin/clone-mac.sh", m], "MAC clone"
        ), thread=True)

    def _wifi_ssid_entered(self, ssid: str | None) -> None:
        if not ssid:
            return
        self.app.push_screen(
            InputModal("WiFi Connect", f"Password for {ssid} (blank = open):", password=True),
            lambda pw, s=ssid: self._do_wifi_connect(s, pw),
        )

    def _do_wifi_connect(self, ssid: str, passwd: str | None) -> None:
        cmd = ["nmcli", "device", "wifi", "connect", ssid, "ifname", "wlan0"]
        if passwd:
            cmd += ["password", passwd]
        self.run_worker(lambda c=cmd, s=ssid: self._run_cmd(c, f"Connect to {s}", timeout=30), thread=True)

    def _check_captive(self) -> None:
        run(["/usr/local/bin/captive-check.sh"], timeout=15)
        active = os.path.exists("/tmp/captive-portal-active")
        msg = "⚠ Captive portal still active — log in via browser" if active else "✓ Internet clear — no captive portal"
        variant = "error" if active else "success"
        self.call_from_thread(
            lambda m=msg, v=variant: self.app.push_screen(MessageModal("Captive Portal", m, v))
        )

    def _run_cmd(self, cmd: list, label: str, timeout: int = 15) -> None:
        rc, out, err = run(cmd, timeout=timeout)
        msg = f"✓ {label} OK\n{out[:200]}" if rc == 0 else f"✗ {label} failed\n{err[:200]}"
        variant = "success" if rc == 0 else "error"
        self.call_from_thread(
            lambda m=msg, v=variant: self.app.push_screen(MessageModal(label, m, v))
        )


# ── Settings screen ───────────────────────────────────────────────────────────
SETTINGS_ITEMS = [
    ("IPHONE_BT_MAC", "iPhone Bluetooth MAC", False),
    ("AP_SSID", "AP Network Name (SSID)", False),
    ("AP_PASSWORD", "AP Password", True),
    ("NTFY_TOPIC", "ntfy.sh Topic", False),
    ("HEADSCALE_URL", "Headscale URL", False),
    ("TAILSCALE_UP_ARGS", "Tailscale Up Arguments", False),
    ("WAN_PING_TARGETS", "WAN Ping Targets", False),
    ("VPN_DEVICE_MACS", "VPN Device MACs", False),
    ("SPLIT_TUNNEL_DOMAINS", "Split Tunnel Domains", False),
    ("AP_CLIENT_BANDWIDTH", "Per-Client Bandwidth", False),
    ("TOR_AP_PASS", "Tor AP Password", True),
    ("MAX_BLOCKLIST_ENTRIES", "Max Blocklist Entries", False),
    ("AP_DISABLE_TIME", "AP Disable Time (HH:MM)", False),
    ("AP_ENABLE_TIME", "AP Enable Time (HH:MM)", False),
    ("UPS_SHUTDOWN_THRESHOLD", "UPS Shutdown Threshold %", False),
    ("PUSHGW_URL", "Prometheus Pushgw URL", False),
    ("SSH_ADMIN_KEY", "SSH Admin Public Key", False),
]


class SettingsScreen(Screen):
    BINDINGS = [Binding("q,escape", "pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Settings  —  select to edit", classes="panel-title")
        yield DataTable(id="settings-table", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#settings-table", DataTable)
        t.add_columns("Setting", "Value")
        self._load_settings()

    def _load_settings(self) -> None:
        cfg = read_defaults()
        ap_ssid = read_hostapd("ssid")
        ap_pass = read_hostapd("wpa_passphrase")
        t = self.query_one("#settings-table", DataTable)
        t.clear()
        for key, label, secret in SETTINGS_ITEMS:
            if key == "AP_SSID":
                val = ap_ssid or "(empty)"
            elif key == "AP_PASSWORD":
                val = f"(set — {len(ap_pass)} chars)" if ap_pass else "(empty)"
            elif secret:
                raw = cfg.get(key, "")
                val = f"(set — {len(raw)} chars)" if raw else "(empty)"
            else:
                raw = cfg.get(key, "")
                val = raw[:40] + "..." if len(raw) > 40 else (raw or "(empty)")
            t.add_row(label, f"[dim]{val}[/dim]")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(SETTINGS_ITEMS):
            key, label, secret = SETTINGS_ITEMS[idx]
            cfg = read_defaults()
            if key == "AP_SSID":
                current = read_hostapd("ssid")
            elif key == "AP_PASSWORD":
                current = read_hostapd("wpa_passphrase")
            else:
                current = cfg.get(key, "")
            self.app.push_screen(
                InputModal(label, f"New value for {label}:", current=current, password=secret),
                lambda v, k=key, l=label: self._save_setting(k, l, v) if v else None,
            )

    def _save_setting(self, key: str, label: str, value: str) -> None:
        self.run_worker(lambda k=key, l=label, v=value: self._save_worker(k, l, v), thread=True)

    def _save_worker(self, key: str, label: str, value: str) -> None:
        err = None
        restart_msg = ""
        if key == "AP_SSID":
            if len(value) < 1 or len(value) > 32:
                err = "SSID must be 1–32 characters"
            elif "#" in value:
                err = "SSID must not contain '#'"
            else:
                err = write_hostapd("ssid", value)
                if not err:
                    rc, _, stderr = run(["systemctl", "restart", "hostapd"])
                    restart_msg = f"\nAP renamed to {value}" if rc == 0 else f"\nhostapd restart failed: {stderr[:60]}"
        elif key == "AP_PASSWORD":
            if len(value) < 8 or len(value) > 63:
                err = "Password must be 8–63 characters"
            elif "#" in value:
                err = "Password must not contain '#'"
            else:
                err = write_hostapd("wpa_passphrase", value)
                if not err:
                    rc, _, stderr = run(["systemctl", "restart", "hostapd"])
                    restart_msg = "\nhostapd restarted" if rc == 0 else f"\nhostapd restart failed: {stderr[:60]}"
        elif key == "SSH_ADMIN_KEY":
            if not re.match(
                r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|"
                r"ecdsa-sha2-nistp521|sk-ssh-ed25519|sk-ecdsa-sha2-nistp256)\s",
                value,
            ):
                err = "Invalid SSH key format"
            else:
                err = write_default(key, value)
                if not err:
                    try:
                        os.makedirs("/root/.ssh", mode=0o700, exist_ok=True)
                        ak = "/root/.ssh/authorized_keys"
                        existing = ""
                        try:
                            with open(ak) as fh:
                                existing = fh.read()
                        except OSError:
                            pass
                        if value not in existing:
                            with open(ak, "a") as fh:
                                fh.write(value + "\n")
                            os.chmod(ak, 0o600)
                            restart_msg = "\nKey added to /root/.ssh/authorized_keys"
                        else:
                            restart_msg = "\nKey already present"
                    except OSError as exc:
                        restart_msg = f"\nCould not write authorized_keys: {exc}"
        else:
            err = write_default(key, value)

        if err:
            self.call_from_thread(
                lambda e=err: self.app.push_screen(MessageModal("Error", e, "error"))
            )
        else:
            msg = f"✓ {label} saved{restart_msg}"
            self.call_from_thread(
                lambda m=msg: self.app.push_screen(MessageModal("Saved", m, "success"))
            )
        self.call_from_thread(self._load_settings)


# ── System screen ─────────────────────────────────────────────────────────────
class SystemScreen(Screen):
    BINDINGS = [Binding("q,escape", "pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("System Actions", classes="panel-title")
        with ScrollableContainer():
            yield Button("Change root password", id="chpw-btn", classes="action")
            yield Button("Reboot now", id="reboot-btn", classes="danger")
            yield Button("Shutdown now", id="shutdown-btn", classes="danger")
            yield Button("Run update-router.sh", id="update-btn", classes="action")
            yield Button("Send daily digest now", id="digest-btn", classes="action")
            yield Button("Reload firewall", id="firewall-btn", classes="action")
            yield Button("Run travel-diagnostic", id="diag-btn", classes="action")
            yield Button("Set up 2FA / TOTP", id="2fa-btn", classes="action")
            yield Button("Update threat-intel blocklists", id="blocklist-btn", classes="action")
            yield Button("Generate bandwidth report", id="bwreport-btn", classes="action")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "chpw-btn":
            self.app.push_screen(
                InputModal("Change Root Password", "New password (min 8 chars):", password=True),
                self._got_new_pw,
            )
        elif bid == "reboot-btn":
            self.app.push_screen(
                ConfirmModal("Reboot", "Reboot the router now?"),
                lambda r: run(["reboot"]) if r else None,
            )
        elif bid == "shutdown-btn":
            self.app.push_screen(
                ConfirmModal("Shutdown", "Shutdown the router now?"),
                lambda r: run(["shutdown", "-h", "now"]) if r else None,
            )
        elif bid == "update-btn":
            self.run_worker(lambda: self._run_long_cmd(
                ["/usr/local/bin/update-router.sh"], "update-router.sh", timeout=300
            ), thread=True)
        elif bid == "digest-btn":
            self.run_worker(lambda: self._run_cmd(
                ["/usr/local/bin/daily-digest.sh"], "Daily digest"
            ), thread=True)
        elif bid == "firewall-btn":
            self.run_worker(lambda: self._run_cmd(
                ["/usr/local/bin/travel-router-firewall.sh", "--save"], "Firewall reload"
            ), thread=True)
        elif bid == "diag-btn":
            self.run_worker(lambda: self._run_long_cmd(
                ["/usr/local/bin/travel-diagnostic"], "travel-diagnostic", timeout=120
            ), thread=True)
        elif bid == "2fa-btn":
            self.run_worker(lambda: self._run_long_cmd(
                ["/usr/local/bin/setup-2fa.sh"], "setup-2fa.sh"
            ), thread=True)
        elif bid == "blocklist-btn":
            self.run_worker(lambda: self._run_long_cmd(
                ["/usr/local/bin/update-blocklists.sh"], "update-blocklists.sh", timeout=120
            ), thread=True)
        elif bid == "bwreport-btn":
            self.run_worker(lambda: self._run_long_cmd(
                ["/usr/local/bin/generate-bandwidth-report.sh"], "Bandwidth report", timeout=60
            ), thread=True)

    def _got_new_pw(self, pw: str | None) -> None:
        if not pw:
            return
        if len(pw) < 8:
            self.app.push_screen(MessageModal("Error", "Password must be at least 8 characters", "error"))
            return
        self.run_worker(lambda p=pw: self._change_password(p), thread=True)

    def _change_password(self, pw: str) -> None:
        try:
            result = subprocess.run(
                ["chpasswd"],
                input=f"root:{pw}\n",
                text=True,
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                self.call_from_thread(
                    lambda: self.app.push_screen(MessageModal("Password", "✓ Root password updated", "success"))
                )
            else:
                self.call_from_thread(
                    lambda e=result.stderr: self.app.push_screen(
                        MessageModal("Password", f"✗ chpasswd failed: {e[:80]}", "error")
                    )
                )
        except Exception as exc:
            self.call_from_thread(
                lambda e=str(exc): self.app.push_screen(MessageModal("Password", f"✗ Error: {e}", "error"))
            )

    def _run_cmd(self, cmd: list, label: str, timeout: int = 15) -> None:
        rc, out, err = run(cmd, timeout=timeout)
        msg = f"✓ {label} OK\n{out[:200]}" if rc == 0 else f"✗ {label} failed\n{err[:200]}"
        variant = "success" if rc == 0 else "error"
        self.call_from_thread(
            lambda m=msg, v=variant: self.app.push_screen(MessageModal(label, m, v))
        )

    def _run_long_cmd(self, cmd: list, label: str, timeout: int = 60) -> None:
        rc, out, err = run(cmd, timeout=timeout)
        combined = (out + err).strip()
        msg = f"✓ {label} completed\n{combined[:400]}" if rc == 0 else f"✗ {label} failed\n{combined[:400]}"
        variant = "success" if rc == 0 else "error"
        self.call_from_thread(
            lambda m=msg, v=variant: self.app.push_screen(MessageModal(label, m, v))
        )


# ── WireGuard screen ──────────────────────────────────────────────────────────
class WireGuardScreen(Screen):
    BINDINGS = [Binding("q,escape", "pop_screen", "Back"), Binding("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("WireGuard", classes="panel-title")
        cfg = read_defaults()
        enabled = cfg.get("ENABLE_WIREGUARD", "0") == "1"
        if not enabled:
            yield Label("[dim]WireGuard is disabled (ENABLE_WIREGUARD=0)[/dim]")
            yield Label("Enable it in Features to use this screen.")
        else:
            yield DataTable(id="wg-table", zebra_stripes=True)
            yield Static(id="wg-status")
        yield Footer()

    def on_mount(self) -> None:
        cfg = read_defaults()
        if cfg.get("ENABLE_WIREGUARD", "0") != "1":
            return
        try:
            t = self.query_one("#wg-table", DataTable)
            t.add_columns("Peer", "Endpoint", "Allowed IPs", "Latest Handshake", "Tx/Rx")
            self._load_wg()
        except Exception:
            pass

    def _load_wg(self) -> None:
        self.run_worker(self._fetch_wg, exclusive=True, thread=True)

    def _fetch_wg(self) -> None:
        cfg = read_defaults()
        iface = cfg.get("WG_INTERFACE", "wg0")
        rc, out, err = run(["wg", "show", iface], timeout=8)
        if rc != 0:
            self.call_from_thread(
                lambda e=err: self.query_one("#wg-status", Static).update(
                    f"[red]wg show failed: {e[:80]}[/red]"
                )
            )
            return

        # Parse wg show output
        peers = []
        current: dict = {}
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("peer:"):
                if current:
                    peers.append(current)
                current = {"peer": line.split(":", 1)[1].strip()}
            elif line.startswith("endpoint:"):
                current["endpoint"] = line.split(":", 1)[1].strip()
            elif line.startswith("allowed ips:"):
                current["allowed_ips"] = line.split(":", 1)[1].strip()
            elif line.startswith("latest handshake:"):
                current["handshake"] = line.split(":", 1)[1].strip()
            elif line.startswith("transfer:"):
                current["transfer"] = line.split(":", 1)[1].strip()

        if current:
            peers.append(current)

        self.call_from_thread(self._apply_wg, peers, out)

    def _apply_wg(self, peers: list, raw: str) -> None:
        try:
            t = self.query_one("#wg-table", DataTable)
            t.clear()
            if not peers:
                t.add_row("(no peers configured)", "", "", "", "")
            else:
                for p in peers:
                    key = p.get("peer", "?")[:20]
                    t.add_row(
                        key,
                        p.get("endpoint", "—"),
                        p.get("allowed_ips", "—"),
                        p.get("handshake", "—"),
                        p.get("transfer", "—"),
                    )
        except Exception:
            pass

    def action_refresh(self) -> None:
        self._load_wg()


# ── Route table screen ────────────────────────────────────────────────────────
class RoutesScreen(Screen):
    BINDINGS = [Binding("q,escape", "pop_screen", "Back"), Binding("r", "refresh_routes", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("Policy Route Tables", classes="panel-title")
        yield DataTable(id="routes-table", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#routes-table", DataTable)
        t.add_columns("Table", "Route")
        self._load_routes()

    def _load_routes(self) -> None:
        self.run_worker(self._fetch_routes, exclusive=True, thread=True)

    def _fetch_routes(self) -> None:
        _, out, _ = run(["ip", "route", "show", "table", "all"], timeout=8)
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # Extract table identifier if present
            m = re.search(r"\btable\s+(\S+)", line)
            table = m.group(1) if m else "main"
            # Filter for policy-relevant tables (skip default main/local clutter)
            if table in ("local",) and "127." in line:
                continue
            rows.append((table, line[:80]))
        self.call_from_thread(self._apply_routes, rows)

    def _apply_routes(self, rows: list) -> None:
        t = self.query_one("#routes-table", DataTable)
        t.clear()
        if not rows:
            t.add_row("(none)", "(no routes found)")
        else:
            for table, route in rows:
                t.add_row(f"[cyan]{table}[/cyan]", route)

    def action_refresh_routes(self) -> None:
        self._load_routes()


# ── Main App ──────────────────────────────────────────────────────────────────
class TravelRouterApp(App):
    TITLE = "Pi Travel Router"
    CSS = APP_CSS

    SCREENS = {
        "dashboard": DashboardScreen,
        "services": ServicesScreen,
        "features": FeaturesScreen,
        "logs": LogsScreen,
        "clients": ClientsScreen,
        "network": NetworkScreen,
        "settings": SettingsScreen,
        "system": SystemScreen,
        "wireguard": WireGuardScreen,
        "routes": RoutesScreen,
    }

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def on_mount(self) -> None:
        self.push_screen("dashboard")


def main() -> None:
    app = TravelRouterApp()
    app.run()


if __name__ == "__main__":
    main()
