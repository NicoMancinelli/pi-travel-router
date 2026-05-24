#!/usr/bin/env bats
# bats file_tags=security,aide,integrity
# Unit tests for scripts/aide-check.sh

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
SCRIPT="${SCRIPT_DIR}/scripts/aide-check.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"

    export LOG_DIR="${_STATE_DIR}/log"
    mkdir -p "${LOG_DIR}"

    # Create a fake aide.conf so the script doesn't fail on --config
    mkdir -p "${_STATE_DIR}/etc/aide"
    export AIDE_CONF="${_STATE_DIR}/etc/aide/aide.conf"
    touch "${AIDE_CONF}"

    # Silent notify mock by default
    mock_cmd "notify-router.sh" "" 0
    mock_cmd "date" "2025-06-15T12:00:00Z" 0
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

# Helper: run the patched script with redirected paths
_run_script() {
    # Patch the script to use test paths (log dir, aide.conf, notify path)
    local tmp="${_STATE_DIR}/aide_test.sh"
    sed \
        -e "s|LOG_DIR=\"/var/log/travel-router\"|LOG_DIR=\"${LOG_DIR}\"|" \
        -e "s|LOG_FILE=\"\${LOG_DIR}/aide.log\"|LOG_FILE=\"${LOG_DIR}/aide.log\"|" \
        -e "s|NOTIFY=\"/usr/local/sbin/notify-router.sh\"|NOTIFY=\"${MOCK_BIN}/notify-router.sh\"|" \
        -e "s|AIDE_CONF=\"/etc/aide/aide.conf\"|AIDE_CONF=\"${AIDE_CONF}\"|" \
        "${SCRIPT}" > "${tmp}"
    chmod +x "${tmp}"
    run bash "${tmp}"
}

# ---------------------------------------------------------------------------
# Test 1: aide not installed → exits 0 silently (no output, no error)
# ---------------------------------------------------------------------------
@test "aide-check: exits 0 silently when aide is not installed" {
    # Ensure 'aide' is not in the mock PATH
    rm -f "${MOCK_BIN}/aide"

    # Patch command -v to return failure for aide
    mock_cmd_script "command" '
        if [ "$1" = "-v" ] && [ "$2" = "aide" ]; then
            exit 1
        fi
        # Fall through for other commands
        exit 0
    '

    # Build a version that does NOT have aide in PATH but does have command mock
    local tmp="${_STATE_DIR}/aide_noinstall.sh"
    sed \
        -e "s|LOG_DIR=\"/var/log/travel-router\"|LOG_DIR=\"${LOG_DIR}\"|" \
        -e "s|NOTIFY=\"/usr/local/sbin/notify-router.sh\"|NOTIFY=\"${MOCK_BIN}/notify-router.sh\"|" \
        -e "s|AIDE_CONF=\"/etc/aide/aide.conf\"|AIDE_CONF=\"${AIDE_CONF}\"|" \
        "${SCRIPT}" > "${tmp}"
    chmod +x "${tmp}"

    # Run with a PATH that does NOT include aide
    run env PATH="${MOCK_BIN}:/usr/bin:/bin" bash "${tmp}"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# Test 2: aide --check returns 0 → exits 0, no notification sent
# ---------------------------------------------------------------------------
@test "aide-check: exits 0 and does not notify when aide finds no changes" {
    mock_cmd "aide" "All files and directories match." 0
    # Capture notify calls
    mock_cmd_script "notify-router.sh" \
        'printf "%s\n" "$*" >> "${MOCK_BIN}/notify.calls"; exit 0'

    _run_script
    [ "$status" -eq 0 ]
    # No notification should have been sent
    [ ! -f "${MOCK_BIN}/notify.calls" ]
}

# ---------------------------------------------------------------------------
# Test 3: aide --check returns non-zero → notify-router.sh called with "integrity"
# ---------------------------------------------------------------------------
@test "aide-check: calls notify-router.sh with 'integrity' when aide finds changes" {
    mock_cmd_script "aide" \
        'printf "File /etc/passwd changed\n"; exit 1'
    mock_cmd_script "notify-router.sh" \
        'printf "%s\n" "$*" >> "${MOCK_BIN}/notify.calls"; exit 0'

    _run_script
    [ "$status" -eq 0 ]
    [ -f "${MOCK_BIN}/notify.calls" ]
    grep -qi "integrity" "${MOCK_BIN}/notify.calls"
}

# ---------------------------------------------------------------------------
# Test 4: Long aide output is truncated to ≤500 chars in notification
# ---------------------------------------------------------------------------
@test "aide-check: truncates long aide output to 500 chars in notification" {
    # Produce output that is definitely longer than 500 chars
    local long_line
    long_line="$(python3 -c "print('X' * 600, end='')")"

    mock_cmd_script "aide" \
        "printf '%s\n' '${long_line}'; exit 2"
    mock_cmd_script "notify-router.sh" \
        'printf "%s\n" "$*" >> "${MOCK_BIN}/notify.calls"; exit 0'

    _run_script
    [ "$status" -eq 0 ]
    [ -f "${MOCK_BIN}/notify.calls" ]
    # The notification line must contain "truncated" marker
    grep -q "truncated" "${MOCK_BIN}/notify.calls"
}

# ---------------------------------------------------------------------------
# Test 5: Script always exits 0 even when aide detects changes
# ---------------------------------------------------------------------------
@test "aide-check: always exits 0 even when aide reports changes (exit code 1)" {
    mock_cmd_script "aide" \
        'printf "Changes detected\n"; exit 1'
    mock_cmd "notify-router.sh" "" 0

    _run_script
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Test 6: aide exits with code 2 (error) — script still exits 0
# ---------------------------------------------------------------------------
@test "aide-check: exits 0 even when aide exits with error code 2" {
    mock_cmd_script "aide" \
        'printf "Database error\n"; exit 2'
    mock_cmd "notify-router.sh" "" 0

    _run_script
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Test 7: When aide finds changes, output appears in the log file
# ---------------------------------------------------------------------------
@test "aide-check: aide output is written to the log file on change detection" {
    mock_cmd_script "aide" \
        'printf "File /etc/hosts changed\n"; exit 1'
    mock_cmd "notify-router.sh" "" 0

    _run_script
    [ "$status" -eq 0 ]
    grep -q "AIDE" "${LOG_DIR}/aide.log"
}
