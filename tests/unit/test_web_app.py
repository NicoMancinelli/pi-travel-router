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
    for required in ("/api/storage", "/api/status", "/api/clients"):
        assert required in urls, f"missing core endpoint {required}"
