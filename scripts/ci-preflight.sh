#!/bin/bash
# ci-preflight.sh — run the same checks CI runs, locally, before push.
# Usage: bash scripts/ci-preflight.sh
# Exit code: 0 = all passed, non-zero = something failed.

set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO}"

PASS=0
FAIL=0

_ok()   { echo "  ✓ $*"; ((PASS++)) || true; }
_fail() { echo "  ✗ $*" >&2; ((FAIL++)) || true; }
_section() { echo; echo "── $* ──────────────────────────────────────────"; }

# ── 1. Bash syntax checks (mirrors build-image.yml smoke-test step) ─────────
_section "Bash syntax checks"

BASH_CHECK_SCRIPTS=(
    install.sh
    build/stage-travel-router/00-setup/01-run.sh
    build/stage-travel-router/files/imager-compat.sh
    scripts/wan-watchdog.sh
    scripts/captive-check.sh
    scripts/failover-watchdog.sh
)

for f in "${BASH_CHECK_SCRIPTS[@]}"; do
    if [ ! -f "${f}" ]; then
        _fail "${f}: file not found"
    elif bash -n "${f}" 2>/dev/null; then
        _ok "${f}"
    else
        _fail "${f}: syntax error"
        bash -n "${f}" || true
    fi
done

# Also check all scripts/ and install/ shell files
while IFS= read -r -d '' f; do
    if bash -n "${f}" 2>/dev/null; then
        _ok "${f}"
    else
        _fail "${f}: syntax error"
        bash -n "${f}" || true
    fi
done < <(find scripts/ install/ -name '*.sh' -print0 2>/dev/null)

# ── 2. Python syntax checks ──────────────────────────────────────────────────
_section "Python syntax checks"

while IFS= read -r -d '' f; do
    if python3 -c "import ast; ast.parse(open('${f}').read())" 2>/dev/null; then
        _ok "${f}"
    else
        _fail "${f}: syntax error"
        python3 -c "import ast; ast.parse(open('${f}').read())" || true
    fi
done < <(find firstboot/ web/ install/lib/ scripts/ -name '*.py' -print0 2>/dev/null)

# ── 3. Shellcheck (mirrors shellcheck CI job) ────────────────────────────────
_section "Shellcheck"

if command -v shellcheck &>/dev/null; then
    # Same files as the shellcheck CI workflow
    SHELLCHECK_TARGETS=()
    while IFS= read -r -d '' f; do
        SHELLCHECK_TARGETS+=("${f}")
    done < <(find scripts/ install/ build/ -name '*.sh' -print0 2>/dev/null)

    SC_FAIL=0
    for f in "${SHELLCHECK_TARGETS[@]}"; do
        if shellcheck -S warning "${f}" 2>/dev/null; then
            true
        else
            _fail "shellcheck: ${f}"
            SC_FAIL=1
        fi
    done
    [ "${SC_FAIL}" -eq 0 ] && _ok "shellcheck: no warnings in scripts/ install/ build/"
else
    echo "  (shellcheck not installed — skipping)"
fi

# ── 4. Python lint (mirrors python-lint.yml) ─────────────────────────────────
_section "Python lint (flake8)"

if command -v flake8 &>/dev/null; then
    # firstboot/: full style gate (mirrors python-lint.yml)
    if flake8 firstboot/ --max-line-length=120 --ignore=E501 2>/dev/null; then
        _ok "flake8 firstboot/: no errors"
    else
        _fail "flake8 firstboot/ errors:"
        flake8 firstboot/ --max-line-length=120 --ignore=E501 || true
    fi
    # web/: correctness gate — syntax errors, all pyflakes checks (unused
    # imports/vars, redefinitions, undefined names), and E7 comparisons.
    # Only deliberate late imports (E402) and W503/E501 style opinions remain.
    if flake8 web/ --max-line-length=120 --select=E7,E9,F 2>/dev/null; then
        _ok "flake8 web/ (correctness): no errors"
    else
        _fail "flake8 web/ correctness errors:"
        flake8 web/ --max-line-length=120 --select=E7,E9,F || true
    fi
else
    echo "  (flake8 not installed — skipping)"
fi

# ── 5. Bats unit tests ───────────────────────────────────────────────────────
_section "Bats unit tests"

if command -v bats &>/dev/null; then
    _bats_out="$(bats tests/unit/ 2>&1)" || { echo "${_bats_out}" | tail -5; _fail "bats: test failures (see above)"; _bats_out=""; }
    [ -n "${_bats_out}" ] && { echo "${_bats_out}" | tail -5; _ok "bats: all tests passed"; }
else
    echo "  (bats not installed — skipping; install with: brew install bats-core)"
fi

# ── 6. Pytest ────────────────────────────────────────────────────────────────
_section "Pytest"

if command -v pytest &>/dev/null; then
    # Capture output so the if tests pytest's exit code, not tail's.
    if _py_out="$(pytest tests/unit/ -q 2>&1)"; then
        echo "${_py_out}" | tail -5
        _ok "pytest: all tests passed"
    else
        echo "${_py_out}" | tail -15
        _fail "pytest: test failures (see above)"
    fi
else
    echo "  (pytest not installed — skipping; install with: pip install pytest)"
fi

# ── 7. Integration tests (mirrors unit-tests.yml, blocking) ──────────────────
_section "Integration tests"

if command -v bats &>/dev/null; then
    if _ibats_out="$(bats tests/integration/ 2>&1)"; then
        echo "${_ibats_out}" | tail -3
        _ok "bats integration: all tests passed"
    else
        echo "${_ibats_out}" | tail -15
        _fail "bats integration: test failures (see above)"
    fi
else
    echo "  (bats not installed — skipping)"
fi

if command -v pytest &>/dev/null; then
    if _ipy_out="$(pytest tests/integration/ -q 2>&1)"; then
        echo "${_ipy_out}" | tail -3
        _ok "pytest integration: all tests passed"
    else
        echo "${_ipy_out}" | tail -15
        _fail "pytest integration: test failures (see above)"
    fi
fi

# ── 8. Key file existence checks ─────────────────────────────────────────────
_section "Key file existence"

REQUIRED_FILES=(
    firstboot/firstboot.service
    build/stage-travel-router/files/imager-compat.sh
    build/stage-travel-router/00-setup/00-packages
    build/stage-travel-router/00-setup/01-run.sh
    build/config
    scripts/travel-tui.py
    web/app.py
    web/static/index.html
)

for f in "${REQUIRED_FILES[@]}"; do
    if [ -f "${f}" ]; then
        _ok "${f} exists"
    else
        _fail "${f}: MISSING"
    fi
done

# ── Summary ──────────────────────────────────────────────────────────────────
echo
echo "────────────────────────────────────────────────"
echo "  Passed: ${PASS}   Failed: ${FAIL}"
echo "────────────────────────────────────────────────"

if [ "${FAIL}" -gt 0 ]; then
    echo "  ❌ Preflight FAILED — fix the above before pushing"
    exit 1
else
    echo "  ✅ All checks passed — safe to push"
    exit 0
fi
