#!/usr/bin/env python3
"""
config.py — Config engine for /etc/default/travel-router

Provides atomic read/write of shell-style KEY=value config files.
All writes are atomic (tempfile + os.replace) and append to a history log.

Usage:
    from install.lib import config

    val = config.get("NTFY_TOPIC")
    config.set("NTFY_TOPIC", "my-router-alerts")

    all_config = config.read_config()
    config.write_config({"ENABLE_DOT": "1", "NTFY_TOPIC": "alerts"})

Environment overrides:
    TRAVEL_ROUTER_CONFIG — path to config file (default: /etc/default/travel-router)
"""

import os
import re
import shlex
import tempfile
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = os.environ.get(
    "TRAVEL_ROUTER_CONFIG", "/etc/default/travel-router"
)
HISTORY_LOG = "/etc/travel-router/history.log"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_config(path: str = CONFIG_PATH) -> dict[str, str]:
    """Return a dict of KEY → value from a shell-style config file.

    Lines matching KEY=value or KEY='value' or KEY="value" are parsed.
    Comment lines (# ...) and blank lines are ignored.
    Values are unquoted via shlex.split so they match what the shell sees.
    """
    result: dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
                if not m:
                    continue
                key, raw_val = m.group(1), m.group(2)
                # Strip inline comments (# ...)
                raw_val = re.sub(r'\s+#.*$', '', raw_val)
                try:
                    parts = shlex.split(raw_val)
                    result[key] = parts[0] if parts else ""
                except ValueError:
                    # Malformed quoting — store raw
                    result[key] = raw_val.strip("'\"")
    except FileNotFoundError:
        pass
    return result


def write_config(updates: dict[str, str], path: str = CONFIG_PATH) -> None:
    """Atomically update keys in config file.

    Preserves comments, blank lines, and the ordering of existing keys.
    Keys that already exist are updated in-place; new keys are appended.
    Uses tempfile + os.replace for atomicity.
    """
    lines: list[str] = []
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        # Create the directory and an empty file if needed
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    updated_keys: set[str] = set()

    def _replace_line(line: str) -> str:
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=', line)
        if not m:
            return line
        key = m.group(1)
        if key in updates:
            updated_keys.add(key)
            return f"{key}={shlex.quote(str(updates[key]))}\n"
        return line

    new_lines = [_replace_line(l) for l in lines]

    # Append keys that were not found in the existing file
    for key, val in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={shlex.quote(str(val))}\n")

    _atomic_write(path, new_lines)
    _append_history(updates)


def get(key: str, default: str | None = None, path: str = CONFIG_PATH) -> str | None:
    """Read a single key from the config file."""
    return read_config(path).get(key, default)


def set(key: str, value: str, path: str = CONFIG_PATH) -> None:  # noqa: A001
    """Set a single key and append to the history log."""
    write_config({key: value}, path)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _atomic_write(path: str, lines: list[str]) -> None:
    """Write lines to path atomically via a temp file in the same directory."""
    dir_path = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=dir_path, prefix=".config-tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.writelines(lines)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_history(updates: dict[str, str]) -> None:
    """Append KEY=value entries to the history log (best-effort)."""
    try:
        Path(HISTORY_LOG).parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(HISTORY_LOG, "a") as fh:
            for key, val in updates.items():
                fh.write(f"{ts} {key}={shlex.quote(str(val))}\n")
    except OSError:
        pass  # Best-effort; never crash on history write


# ---------------------------------------------------------------------------
# CLI shim (useful for shell scripts: python3 -m install.lib.config get KEY)
# ---------------------------------------------------------------------------

def _main() -> None:
    import sys

    def _usage() -> None:
        print(
            "Usage: python3 config.py get KEY [default]\n"
            "       python3 config.py set KEY VALUE\n"
            "       python3 config.py dump",
            file=sys.stderr,
        )
        sys.exit(1)

    if len(sys.argv) < 2:
        _usage()

    cmd = sys.argv[1]
    if cmd == "get":
        if len(sys.argv) < 3:
            _usage()
        key = sys.argv[2]
        default = sys.argv[3] if len(sys.argv) > 3 else None
        val = get(key, default)
        if val is None:
            sys.exit(1)
        print(val)
    elif cmd == "set":
        if len(sys.argv) < 4:
            _usage()
        set(sys.argv[2], sys.argv[3])
    elif cmd == "dump":
        for k, v in read_config().items():
            print(f"{k}={shlex.quote(v)}")
    else:
        _usage()


if __name__ == "__main__":
    _main()
