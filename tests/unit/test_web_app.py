"""
Smoke tests for web/app.py — the management dashboard Flask app.

The app once shipped with duplicate route function names, which makes Flask
raise AssertionError at import time and takes the whole dashboard down.
CI integration tests were non-blocking, so nothing caught it. These tests
are the blocking guard:

 1. The module imports (no syntax errors, no duplicate Flask endpoints).
 2. No two routes register the same (URL, methods) pair.
 3. A sane number of routes is registered.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

_WEB_DIR = Path(__file__).parent.parent.parent / "web"
sys.path.insert(0, str(_WEB_DIR))

import app as web_app  # noqa: E402  (after sys.path manipulation)


def _rules():
    return [
        r for r in web_app.app.url_map.iter_rules()
        if r.rule != "/static/<path:filename>"
    ]


def test_app_imports_and_registers_routes():
    """Importing the module must succeed and register the API surface."""
    assert len(_rules()) > 100


def test_no_duplicate_url_method_pairs():
    """No two view functions may claim the same URL + method combination."""
    pairs = Counter(
        (r.rule, m)
        for r in _rules()
        for m in (r.methods - {"HEAD", "OPTIONS"})
    )
    dupes = {pair: n for pair, n in pairs.items() if n > 1}
    assert not dupes, f"duplicate route registrations: {dupes}"


def test_core_endpoints_present():
    """Endpoints the TUI and dashboard depend on must exist."""
    urls = {r.rule for r in _rules()}
    for required in ("/api/storage", "/api/status", "/api/clients",
                     "/api/system/load-history"):
        assert required in urls, f"missing core endpoint {required}"


def test_run_helper_returns_two_tuple():
    """_run must return exactly (stdout, returncode) — guards the 3-tuple unpack bug."""
    out = web_app._run(["true"])
    assert isinstance(out, tuple) and len(out) == 2
    stdout, rc = out
    assert isinstance(stdout, str)
    assert rc == 0


def test_no_endpoint_mis_unpacks_run():
    """No endpoint may unpack _run() into 3 names (it returns a 2-tuple)."""
    import re
    from pathlib import Path
    src = (Path(__file__).parent.parent.parent / "web" / "app.py").read_text()
    bad = re.findall(r"\w+,\s*\w+,\s*\w+\s*=\s*_run\(", src)
    assert not bad, f"3-tuple _run unpack(s) found: {bad}"


def test_deduped_endpoints_satisfy_dashboard_contract():
    """Endpoints whose duplicates were removed must still return the keys the
    dashboard cards read (success path), so cards don't render empty."""
    import tempfile
    from pathlib import Path

    tok = "contract-test-token"
    tf = tempfile.NamedTemporaryFile("w", delete=False)
    tf.write(tok)
    tf.close()
    web_app.WEB_TOKEN_FILE = tf.name
    client = web_app.app.test_client()
    hdr = {"Authorization": f"Bearer {tok}"}

    # (endpoint, keys the dashboard reads on the success path)
    contracts = {
        "/api/system/sysctl-security": {"values", "secure", "issues"},
        "/api/system/pi-hardware": {"core_count", "ram_mb", "is_pi"},
        "/api/network/routes": {"default_gw", "routes", "count"},
        "/api/system/load-avg": {"cpu_count", "load_1m", "status"},
    }
    for ep, keys in contracts.items():
        body = client.get(ep, headers=hdr).get_json()
        assert isinstance(body, dict), f"{ep} did not return a JSON object"
        missing = keys - set(body)
        assert not missing, f"{ep} missing dashboard keys: {missing}"
