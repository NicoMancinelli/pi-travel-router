#!/bin/bash
# install/lib/common.sh — shared helpers sourced by all install modules
# Source this file; do not execute it directly.

# ── Colour helpers (match install.sh palette) ──────────────────────────────────
_TR_R='\033[0;31m'
_TR_G='\033[0;32m'
_TR_Y='\033[1;33m'
_TR_C='\033[0;36m'
_TR_NC='\033[0m'

log()       { echo -e "${_TR_C}→${_TR_NC} $*"; }
log_ok()    { echo -e "${_TR_G}✓${_TR_NC} $*"; }
log_warn()  { echo -e "${_TR_Y}⚠${_TR_NC} $*" >&2; }
log_error() { echo -e "${_TR_R}✗${_TR_NC} $*" >&2; }
section()   { echo -e "\n${_TR_C}━━ $* ━━${_TR_NC}"; }
die()       { echo -e "${_TR_R}✗ FATAL:${_TR_NC} $*" >&2; exit 1; }

# Aliases matching install.sh function names so modules are drop-in compatible
ok()        { log_ok "$@"; }
info()      { log "$@"; }
warn()      { log_warn "$@"; }

# ── Dry-run support ─────────────────────────────────────────────────────────────
# Set DRY_RUN=1 to print commands instead of executing them.
is_dry_run() { [[ "${DRY_RUN:-0}" == "1" ]]; }

run_or_dry() {
    if is_dry_run; then
        log "[DRY-RUN] $*"
        return 0
    fi
    "$@"
}

# ── install_file helper (mirrors install.sh) ────────────────────────────────────
# install_file <repo-relative-src> <dest> [mode]
# REPO must be set by the caller (install/run.sh sets it).
install_file() {
    local src="${REPO}/$1" dst="$2" mode="${3:-644}"
    mkdir -p "$(dirname "$dst")"
    if is_dry_run; then
        log "[DRY-RUN] install_file $src → $dst (mode $mode)"
        return 0
    fi
    cp "$src" "$dst"
    chmod "$mode" "$dst"
}

# ── _safe_write_conf (mirrors install.sh C11) ──────────────────────────────────
# Safely rewrite a key=value line using Python to avoid shell metacharacter issues.
_safe_write_conf() {
    local key="$1" val="$2" path="$3"
    if is_dry_run; then
        log "[DRY-RUN] _safe_write_conf $key=<value> in $path"
        return 0
    fi
    python3 -c "
import sys, re, shlex, os, tempfile
key, val, path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f: lines = f.readlines()
pat = re.compile(r'^' + re.escape(key) + r'=')
new_line = key + '=' + shlex.quote(val) + '\n'
lines = [new_line if pat.match(l) else l for l in lines]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
try:
    with os.fdopen(fd, 'w') as fh: fh.writelines(lines)
    os.replace(tmp, path)
except:
    os.unlink(tmp); raise
" "$key" "$val" "$path"
}

# ── Timestamp ──────────────────────────────────────────────────────────────────
timestamp() { date -u +%H:%M:%S; }
