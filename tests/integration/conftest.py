"""
pytest conftest for integration tests.

Sets up environment variables and file stubs so that web/app.py can be
imported and run without real Pi hardware or system files.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import threading
from pathlib import Path

import pytest

# Repository root is two levels up from this file (tests/integration/)
_REPO_ROOT = Path(__file__).parent.parent.parent
_WEB_DIR = _REPO_ROOT / "web"


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def tmp_fs(tmp_path_factory):
    """Session-scoped tmp directory for fake system files."""
    return tmp_path_factory.mktemp("pi_fs")


@pytest.fixture(scope="session")
def api_server(tmp_fs):
    """
    Start the Flask app (web/app.py) on a random port in a background thread.

    Yields a dict with:
        base_url  – http://127.0.0.1:<port>
        token     – the Bearer token for authenticated requests
        tmp_fs    – path to the session tmp directory
    """
    # ── Fake filesystem stubs ──────────────────────────────────────────────
    token = "test-integration-token-abc123"
    token_file = tmp_fs / "web-token"
    token_file.write_text(token + "\n")

    defaults_file = tmp_fs / "travel-router"
    defaults_file.write_text(
        "ENABLE_HOTSPOT=1\n"
        "ENABLE_WIREGUARD=0\n"
        "WG_LISTEN_PORT=51820\n"
    )

    wg_conf = tmp_fs / "wg0.conf"
    wg_conf.write_text("[Interface]\nPrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n")

    log_dir = tmp_fs / "log"
    log_dir.mkdir(exist_ok=True)
    combined_log = log_dir / "combined.log"
    combined_log.write_text("")

    privacy_state_dir = tmp_fs / "privacy"
    privacy_state_dir.mkdir(exist_ok=True)

    # ── Patch app module constants before import ───────────────────────────
    # We add web/ to sys.path and import app, then monkey-patch its constants.
    sys.path.insert(0, str(_WEB_DIR))

    # Set env vars that subprocess calls inside app.py might read
    os.environ.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))

    import importlib
    if "app" in sys.modules:
        app_module = sys.modules["app"]
    else:
        import app as app_module  # noqa: F401

    # Redirect file paths to tmp_fs
    app_module.WEB_TOKEN_FILE = str(token_file)
    app_module.DEFAULTS_FILE = str(defaults_file)
    app_module.COMBINED_LOG = str(combined_log)
    app_module.WG_CONF = str(wg_conf)
    app_module.UPS_STATUS_FILE = str(tmp_fs / "ups-status")
    app_module._PRIVACY_STATE_FILE = str(privacy_state_dir / "privacy-profile")

    # ── Start Flask in a daemon thread ─────────────────────────────────────
    port = find_free_port()
    flask_app = app_module.app
    flask_app.config["TESTING"] = True

    server_thread = threading.Thread(
        target=lambda: flask_app.run(
            host="127.0.0.1",
            port=port,
            debug=False,
            use_reloader=False,
            threaded=True,
        ),
        daemon=True,
    )
    server_thread.start()

    # Wait until the server is accepting connections (up to 5 s)
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        pytest.fail(f"Flask server did not start on port {port} within 5 seconds")

    yield {
        "base_url": base_url,
        "token": token,
        "tmp_fs": tmp_fs,
        "app_module": app_module,
    }
    # Thread is daemon — it dies with the process; no explicit teardown needed.
