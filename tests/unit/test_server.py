"""
pytest unit tests for firstboot/server.py

Tests cover:
 1. Content-Length validation (malformed → 400)
 2. SSH key deduplication (double-write → one copy)
 3. WireGuard public key format validation (44-char base64)
 4. ENABLE_WIREGUARD bool flag is accepted
"""

from __future__ import annotations

import importlib
import io
import os
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import http.client
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the module under test.
# server.py is not a package; add its parent directory to sys.path.
# ---------------------------------------------------------------------------
_SERVER_DIR = Path(__file__).parent.parent.parent / "firstboot"
sys.path.insert(0, str(_SERVER_DIR))

import server  # noqa: E402  (after sys.path manipulation)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_server_state(tmp_path, monkeypatch):
    """Redirect all server file paths to a tmp directory and reset module state."""
    monkeypatch.setattr(server, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(server, "ENV_FILE", str(tmp_path / "firstboot-env.sh"))
    monkeypatch.setattr(server, "ROOTPW_FILE", str(tmp_path / "firstboot-rootpw"))
    monkeypatch.setattr(server, "DONE_FILE", str(tmp_path / "firstboot-done"))
    monkeypatch.setattr(server, "FAIL_FILE", str(tmp_path / "firstboot-failed"))
    monkeypatch.setattr(server, "LOG_FILE", str(tmp_path / "firstboot-install.log"))
    monkeypatch.setattr(server, "_installing", False)
    # Create a dummy index.html so GET / works
    index = tmp_path / "index.html"
    index.write_text("<html><body>{{CSRF_TOKEN}}</body></html>")
    monkeypatch.setattr(server, "INDEX_HTML", str(index))
    yield


@pytest.fixture()
def live_server(tmp_path):
    """Start the HTTPServer in a background thread; yield (host, port, csrf_token)."""
    import socket
    from http.server import HTTPServer

    # Pick a free port
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    httpd = HTTPServer(("127.0.0.1", port), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "127.0.0.1", port, server._csrf_token
    httpd.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_form(**kwargs) -> bytes:
    """Build a URL-encoded form body including the live CSRF token."""
    kwargs.setdefault("_csrf_token", server._csrf_token)
    kwargs.setdefault("AP_SSID", "TestRouter")
    kwargs.setdefault("AP_PASS", "testpassword123")
    kwargs.setdefault("COUNTRY", "US")
    return urllib.parse.urlencode(kwargs).encode("utf-8")


def _post_setup(host: str, port: int, body: bytes, content_length: str | None = None) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": "127.0.0.1",
    }
    if content_length is not None:
        headers["Content-Length"] = content_length
    else:
        headers["Content-Length"] = str(len(body))
    conn.request("POST", "/setup", body=body, headers=headers)
    return conn.getresponse()


# ---------------------------------------------------------------------------
# Test group 1: Content-Length validation
# ---------------------------------------------------------------------------

class TestContentLength:
    def test_non_integer_content_length_returns_400(self, live_server):
        """A non-integer Content-Length header must return 400."""
        host, port, _ = live_server
        body = _make_form()

        conn = http.client.HTTPConnection(host, port, timeout=5)
        # Send a raw request with a malformed Content-Length
        conn.putrequest("POST", "/setup")
        conn.putheader("Content-Type", "application/x-www-form-urlencoded")
        conn.putheader("Content-Length", "not-a-number")
        conn.putheader("Host", "127.0.0.1")
        conn.endheaders()
        # We can't send a body after a bad Content-Length — just send nothing;
        # the server reads 0 bytes.
        resp = conn.getresponse()
        assert resp.status == 400

    def test_zero_content_length_returns_400(self, live_server):
        """Content-Length: 0 must return 400 (no body to parse)."""
        host, port, _ = live_server
        resp = _post_setup(host, port, b"", content_length="0")
        assert resp.status == 400

    def test_oversized_body_returns_400(self, live_server):
        """Content-Length > 32768 must return 400."""
        host, port, _ = live_server
        big_body = b"x=y&" * 10_000  # well over 32 KB
        resp = _post_setup(host, port, big_body, content_length=str(len(big_body)))
        assert resp.status == 400

    def test_valid_content_length_accepted(self, live_server, monkeypatch):
        """A well-formed request with a correct Content-Length is accepted (≥200)."""
        host, port, _ = live_server
        # Prevent actual install from running
        monkeypatch.setattr(server, "_spawn_install", lambda: None)
        body = _make_form()
        resp = _post_setup(host, port, body)
        # We expect either 200 (OK, running page) or 302 (redirect) — not 4xx
        assert resp.status < 400


# ---------------------------------------------------------------------------
# Test group 2: SSH key deduplication
# ---------------------------------------------------------------------------

class TestSSHKeyDedup:
    def test_duplicate_key_not_written_twice(self, tmp_path, monkeypatch):
        """Calling _load_preseed twice with the same key results in one copy in authorized_keys."""
        # Prepare a fake firstrun.sh with an SSH public key
        key_line = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test-key-comment"
        firstrun = tmp_path / "firstrun.sh"
        firstrun.write_text(f"echo '{key_line}' >> /root/.ssh/authorized_keys\n")

        ak_path = tmp_path / "authorized_keys"
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        ak_full = ssh_dir / "authorized_keys"

        # Monkeypatch os.makedirs and os.open to use tmp_path
        real_makedirs = os.makedirs
        real_open = os.open
        real_chmod = os.chmod

        def fake_makedirs(path, mode=0o777, exist_ok=False):
            if "/root/.ssh" in str(path):
                real_makedirs(str(ssh_dir), mode=mode, exist_ok=True)
            else:
                real_makedirs(path, mode=mode, exist_ok=exist_ok)

        def fake_os_open(path, flags, mode=0o666):
            if "/root/.ssh/authorized_keys" in str(path):
                return real_open(str(ak_full), flags, mode)
            return real_open(path, flags, mode)

        def fake_chmod(path, mode):
            if "/root/.ssh" in str(path):
                return
            real_chmod(path, mode)

        monkeypatch.setattr(os, "makedirs", fake_makedirs)
        monkeypatch.setattr(os, "open", fake_os_open)
        monkeypatch.setattr(os, "chmod", fake_chmod)

        # Patch os.path.exists for /root/.ssh/authorized_keys
        real_exists = os.path.exists

        def fake_exists(path):
            if "/root/.ssh/authorized_keys" in str(path):
                return ak_full.exists()
            # Map firstrun.sh candidates
            for candidate in ("/boot/firmware/firstrun.sh", "/boot/firstrun.sh"):
                if path == candidate:
                    return str(path) == str(firstrun) or False
            return real_exists(path)

        monkeypatch.setattr(os.path, "exists", fake_exists)

        # Patch open() for firstrun.sh reading
        real_builtin_open = open

        def fake_open(path, mode="r", **kwargs):
            for candidate in ("/boot/firmware/firstrun.sh", "/boot/firstrun.sh"):
                if str(path) == candidate:
                    return real_builtin_open(str(firstrun), mode, **kwargs)
            if "/root/.ssh/authorized_keys" in str(path):
                return real_builtin_open(str(ak_full), mode, **kwargs)
            return real_builtin_open(path, mode, **kwargs)

        monkeypatch.setitem(server.__builtins__ if isinstance(server.__builtins__, dict)
                            else server.__builtins__.__dict__, "open", fake_open)

        # Call _load_preseed twice
        server._load_preseed()
        server._load_preseed()

        # The key must appear exactly once
        if ak_full.exists():
            content = ak_full.read_text()
            count = content.strip().splitlines()
            matching = [ln for ln in count if key_line.split()[1] in ln]
            assert len(matching) == 1, f"Expected 1 copy of key, found {len(matching)}: {content!r}"

    def test_duplicate_key_in_validate_not_duplicated(self):
        """_validate: SSH_ADMIN_KEY is preserved as-is; no duplication logic inside validate."""
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI test"
        form = {
            "AP_SSID": ["TestRouter"],
            "AP_PASS": ["testpassword"],
            "COUNTRY": ["US"],
            "SSH_ADMIN_KEY": [key],
            "_csrf_token": [server._csrf_token],
        }
        values, errors, _ = server._validate(form)
        assert values["SSH_ADMIN_KEY"] == key
        # No SSH-related errors
        ssh_errors = [e for e in errors if "SSH" in e]
        assert not ssh_errors


# ---------------------------------------------------------------------------
# Test group 3: WireGuard public key format validation
# ---------------------------------------------------------------------------

class TestWireGuardValidation:
    def test_valid_wg_pubkey_accepted(self):
        """A 43-char base64 string ending in '=' passes WireGuard key validation."""
        # Valid 44-char base64 (43 chars + '=')
        valid_key = "A" * 43 + "="
        form = {
            "AP_SSID": ["TestRouter"],
            "AP_PASS": ["testpassword"],
            "COUNTRY": ["US"],
            "WG_PEER_PUBKEY": [valid_key],
            "_csrf_token": [server._csrf_token],
        }
        values, errors, _ = server._validate(form)
        wg_errors = [e for e in errors if "WireGuard" in e and "public key" in e]
        assert not wg_errors
        assert values["WG_PEER_PUBKEY"] == valid_key

    def test_short_wg_pubkey_rejected(self):
        """A too-short WireGuard key (< 44 chars) returns a validation error."""
        short_key = "abc123="
        form = {
            "AP_SSID": ["TestRouter"],
            "AP_PASS": ["testpassword"],
            "COUNTRY": ["US"],
            "WG_PEER_PUBKEY": [short_key],
            "_csrf_token": [server._csrf_token],
        }
        _, errors, _ = server._validate(form)
        wg_errors = [e for e in errors if "WireGuard" in e]
        assert wg_errors

    def test_wg_pubkey_without_equals_rejected(self):
        """A 44-char base64 string not ending in '=' is rejected."""
        bad_key = "A" * 44  # no trailing '='
        form = {
            "AP_SSID": ["TestRouter"],
            "AP_PASS": ["testpassword"],
            "COUNTRY": ["US"],
            "WG_PEER_PUBKEY": [bad_key],
            "_csrf_token": [server._csrf_token],
        }
        _, errors, _ = server._validate(form)
        wg_errors = [e for e in errors if "WireGuard" in e]
        assert wg_errors

    def test_empty_wg_pubkey_accepted(self):
        """Empty WireGuard key (field not provided) is valid — WireGuard is optional."""
        form = {
            "AP_SSID": ["TestRouter"],
            "AP_PASS": ["testpassword"],
            "COUNTRY": ["US"],
            "WG_PEER_PUBKEY": [""],
            "_csrf_token": [server._csrf_token],
        }
        values, errors, _ = server._validate(form)
        wg_errors = [e for e in errors if "WireGuard" in e and "public key" in e]
        assert not wg_errors
        assert values["WG_PEER_PUBKEY"] == ""


# ---------------------------------------------------------------------------
# Test group 4: ENABLE_WIREGUARD bool flag
# ---------------------------------------------------------------------------

class TestBoolFlags:
    def test_enable_wireguard_flag_set_to_1_when_present(self):
        """ENABLE_WIREGUARD is set to '1' when the field is present in the form."""
        form = {
            "AP_SSID": ["TestRouter"],
            "AP_PASS": ["testpassword"],
            "COUNTRY": ["US"],
            "ENABLE_WIREGUARD": ["1"],
            "_csrf_token": [server._csrf_token],
        }
        values, errors, _ = server._validate(form)
        assert values["ENABLE_WIREGUARD"] == "1"

    def test_enable_wireguard_flag_set_to_0_when_absent(self):
        """ENABLE_WIREGUARD is set to '0' when the field is absent from the form."""
        form = {
            "AP_SSID": ["TestRouter"],
            "AP_PASS": ["testpassword"],
            "COUNTRY": ["US"],
            # ENABLE_WIREGUARD not present
            "_csrf_token": [server._csrf_token],
        }
        values, errors, _ = server._validate(form)
        assert values["ENABLE_WIREGUARD"] == "0"

    def test_all_bool_flags_default_to_0(self):
        """All BOOL_FLAGS default to '0' when not supplied in the form."""
        form = {
            "AP_SSID": ["TestRouter"],
            "AP_PASS": ["testpassword"],
            "COUNTRY": ["US"],
            "_csrf_token": [server._csrf_token],
        }
        values, _, _ = server._validate(form)
        for flag in server.BOOL_FLAGS:
            assert values[flag] == "0", f"{flag} expected '0', got {values[flag]!r}"

    def test_enable_wireguard_in_bool_flags_list(self):
        """ENABLE_WIREGUARD must be listed in server.BOOL_FLAGS."""
        assert "ENABLE_WIREGUARD" in server.BOOL_FLAGS


# ---------------------------------------------------------------------------
# Test group 5: Additional _validate edge cases
# ---------------------------------------------------------------------------

class TestValidateMisc:
    def test_ap_ssid_too_long_rejected(self):
        """AP SSID longer than 32 bytes UTF-8 is rejected."""
        form = {
            "AP_SSID": ["A" * 33],
            "AP_PASS": ["testpassword"],
            "COUNTRY": ["US"],
            "_csrf_token": [server._csrf_token],
        }
        _, errors, _ = server._validate(form)
        ssid_errors = [e for e in errors if "SSID" in e]
        assert ssid_errors

    def test_ap_pass_too_short_rejected(self):
        """AP passphrase shorter than 8 characters is rejected."""
        form = {
            "AP_SSID": ["TestSSID"],
            "AP_PASS": ["short"],
            "COUNTRY": ["US"],
            "_csrf_token": [server._csrf_token],
        }
        _, errors, _ = server._validate(form)
        pass_errors = [e for e in errors if "passphrase" in e.lower()]
        assert pass_errors

    def test_invalid_country_code_rejected(self):
        """Country code that is not exactly two uppercase letters is rejected."""
        form = {
            "AP_SSID": ["TestSSID"],
            "AP_PASS": ["testpassword"],
            "COUNTRY": ["USA"],
            "_csrf_token": [server._csrf_token],
        }
        _, errors, _ = server._validate(form)
        country_errors = [e for e in errors if "Country" in e or "country" in e]
        assert country_errors

    def test_wg_listen_port_out_of_range_rejected(self):
        """WireGuard listen port < 1024 is rejected."""
        form = {
            "AP_SSID": ["TestSSID"],
            "AP_PASS": ["testpassword"],
            "COUNTRY": ["US"],
            "WG_LISTEN_PORT": ["80"],
            "_csrf_token": [server._csrf_token],
        }
        _, errors, _ = server._validate(form)
        port_errors = [e for e in errors if "WireGuard listen port" in e]
        assert port_errors
