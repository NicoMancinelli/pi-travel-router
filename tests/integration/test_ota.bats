#!/usr/bin/env bats
# Integration tests for scripts/ota-update.sh
# All external commands (curl, xz, dd, gpg, sha256sum) are mocked.
# The script uses set -euo pipefail so failures propagate correctly.
# /proc/cmdline (slot detection) is spoofed via a wrapper script.

load '../helpers/mock_commands'

REPO_ROOT="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
SCRIPT="${REPO_ROOT}/scripts/ota-update.sh"

setup() {
    setup_mock_bin

    # Writable temp for test artifacts
    export _WORK_DIR
    _WORK_DIR="$(mktemp -d)"

    # Fake /proc to satisfy slot detection
    # The script does: grep -oE 'root=/dev/[^ ]+' /proc/cmdline
    # We mock grep to intercept /proc/cmdline reads and return mmcblk0p2
    cat > "${MOCK_BIN}/grep" <<'MOCK'
#!/bin/bash
# Intercept /proc/cmdline reads for slot detection
_last_arg="${@: -1}"
if [ "${_last_arg}" = "/proc/cmdline" ]; then
    printf 'root=/dev/mmcblk0p2\n'
else
    /usr/bin/grep "$@" 2>/dev/null || /bin/grep "$@" 2>/dev/null || true
fi
MOCK
    chmod +x "${MOCK_BIN}/grep"

    # mktemp: pass through
    cat > "${MOCK_BIN}/mktemp" <<'MOCK'
#!/bin/bash
/usr/bin/mktemp "$@"
MOCK
    chmod +x "${MOCK_BIN}/mktemp"

    # gpg: default to failing (no sig file will be fetched)
    mock_cmd "gpg" "" 1
}

teardown() {
    teardown_mock_bin
    # Remove work dir using absolute path to avoid mocked rm
    /bin/rm -rf "${_WORK_DIR}"
}

# ---------------------------------------------------------------------------
# Helper: run ota-update.sh inside a subshell with the mock PATH
# The _WORK_DIR is where test artifacts land.
# ---------------------------------------------------------------------------
_run_ota() {
    local url="${1:-https://example.com/update.img.xz}"
    run env PATH="${MOCK_BIN}:${PATH}" \
        bash "${SCRIPT}" "${url}"
}

# ---------------------------------------------------------------------------
# Test 1: SHA256 mismatch → script exits non-zero, dd not called
# ---------------------------------------------------------------------------
@test "SHA256 mismatch: script exits non-zero and dd is not called" {
    local dd_calls="${_WORK_DIR}/dd.calls"

    # curl: image download succeeds, .sha256 succeeds, .sig fails
    cat > "${MOCK_BIN}/curl" <<MOCK
#!/bin/bash
_out=""
for _i in "\$@"; do
    if [ "\${_prev}" = "-o" ]; then
        _out="\${_i}"
    fi
    _prev="\${_i}"
done
case "\${_out}" in
    *.sig)   exit 1 ;;
    *.sha256)
        printf 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef  update.img\n' > "\${_out}"
        exit 0 ;;
    *.img.xz)
        printf 'fakexzdata' > "\${_out}"
        exit 0 ;;
    *)  exit 0 ;;
esac
MOCK
    chmod +x "${MOCK_BIN}/curl"

    # xz: decompress to known content
    cat > "${MOCK_BIN}/xz" <<'MOCK'
#!/bin/bash
printf 'FAKEIMAGEDATA'
MOCK
    chmod +x "${MOCK_BIN}/xz"

    # sha256sum: return a hash that does NOT match the expected value
    cat > "${MOCK_BIN}/sha256sum" <<'MOCK'
#!/bin/bash
printf '0000000000000000000000000000000000000000000000000000000000000000  -\n'
MOCK
    chmod +x "${MOCK_BIN}/sha256sum"

    # dd: record if called
    cat > "${MOCK_BIN}/dd" <<MOCK
#!/bin/bash
printf 'dd_called\n' >> "${dd_calls}"
cat > /dev/null
exit 0
MOCK
    chmod +x "${MOCK_BIN}/dd"

    _run_ota

    # Must exit non-zero (SHA256 mismatch is fatal)
    [ "$status" -ne 0 ]

    # dd must NOT have been called
    [ ! -f "${dd_calls}" ]
}

# ---------------------------------------------------------------------------
# Test 2: SHA256 match → dd is called
# ---------------------------------------------------------------------------
@test "SHA256 match: dd is called (image written to inactive slot)" {
    local dd_calls="${_WORK_DIR}/dd.calls"
    local fake_content="CORRECT_IMAGE_CONTENT"

    # Compute the expected hash using whatever tool is available
    local expected_sha
    if command -v sha256sum >/dev/null 2>&1; then
        expected_sha="$(printf '%s' "${fake_content}" | sha256sum | awk '{print $1}')"
    else
        expected_sha="$(printf '%s' "${fake_content}" | shasum -a 256 | awk '{print $1}')"
    fi

    cat > "${MOCK_BIN}/curl" <<MOCK
#!/bin/bash
_out=""
for _i in "\$@"; do
    if [ "\${_prev}" = "-o" ]; then
        _out="\${_i}"
    fi
    _prev="\${_i}"
done
case "\${_out}" in
    *.sig)   exit 1 ;;
    *.sha256)
        printf '%s  update.img\n' "${expected_sha}" > "\${_out}"
        exit 0 ;;
    *.img.xz)
        printf 'fakexzdata' > "\${_out}"
        exit 0 ;;
    *)  exit 0 ;;
esac
MOCK
    chmod +x "${MOCK_BIN}/curl"

    cat > "${MOCK_BIN}/xz" <<MOCK
#!/bin/bash
printf '%s' "${fake_content}"
MOCK
    chmod +x "${MOCK_BIN}/xz"

    # sha256sum mock: return the matching hash
    cat > "${MOCK_BIN}/sha256sum" <<MOCK
#!/bin/bash
printf '%s  -\n' "${expected_sha}"
MOCK
    chmod +x "${MOCK_BIN}/sha256sum"

    cat > "${MOCK_BIN}/dd" <<MOCK
#!/bin/bash
printf 'dd_called\n' >> "${dd_calls}"
cat > /dev/null
exit 0
MOCK
    chmod +x "${MOCK_BIN}/dd"

    _run_ota

    # dd must have been called (image was written)
    [ -f "${dd_calls}" ]
    grep -q "dd_called" "${dd_calls}"
}

# ---------------------------------------------------------------------------
# Test 3: Missing .sha256 → script warns but continues; dd is called
# ---------------------------------------------------------------------------
@test "missing .sha256 file: script warns but continues and calls dd" {
    local dd_calls="${_WORK_DIR}/dd.calls"

    cat > "${MOCK_BIN}/curl" <<MOCK
#!/bin/bash
_out=""
for _i in "\$@"; do
    if [ "\${_prev}" = "-o" ]; then
        _out="\${_i}"
    fi
    _prev="\${_i}"
done
case "\${_out}" in
    *.sig)   exit 1 ;;
    *.sha256) exit 1 ;;   # No SHA256 manifest
    *.img.xz)
        printf 'fakexzdata' > "\${_out}"
        exit 0 ;;
    *)  exit 0 ;;
esac
MOCK
    chmod +x "${MOCK_BIN}/curl"

    cat > "${MOCK_BIN}/xz" <<'MOCK'
#!/bin/bash
printf 'FAKEIMAGE'
MOCK
    chmod +x "${MOCK_BIN}/xz"

    cat > "${MOCK_BIN}/dd" <<MOCK
#!/bin/bash
printf 'dd_called\n' >> "${dd_calls}"
cat > /dev/null
exit 0
MOCK
    chmod +x "${MOCK_BIN}/dd"

    _run_ota

    # dd must have been called (script continued despite missing SHA256)
    [ -f "${dd_calls}" ]
    grep -q "dd_called" "${dd_calls}"

    # Output must contain the warning about missing SHA256
    [[ "$output" == *"WARNING"*  ]] || \
    [[ "$output" == *"No SHA256"* ]] || \
    [[ "$output" == *"skipping"* ]]
}

# ---------------------------------------------------------------------------
# Test 4: No internet — curl fails for image download → non-zero exit
# ---------------------------------------------------------------------------
@test "no internet: curl fails on image download, script exits non-zero" {
    # All curl calls fail
    mock_cmd "curl" "" 1

    _run_ota

    [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# Test 5: No release URL auto-detected → error exit
# ---------------------------------------------------------------------------
@test "no release URL from GitHub API: exits non-zero with error" {
    # curl returns empty (GitHub API returns no .img.xz URL)
    mock_cmd "curl" "" 0

    # Run without an explicit URL argument
    run env PATH="${MOCK_BIN}:${PATH}" bash "${SCRIPT}"

    [ "$status" -ne 0 ]
    [[ "$output" == *"ERROR"* ]]
}
