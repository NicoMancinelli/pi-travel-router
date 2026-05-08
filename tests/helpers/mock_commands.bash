#!/usr/bin/env bash
# Shared bats helper: stubs out system commands for unit testing.
# Usage: load '../helpers/mock_commands'

setup_mock_bin() {
    export MOCK_BIN
    MOCK_BIN="$(mktemp -d)"
    export PATH="${MOCK_BIN}:${PATH}"
}

teardown_mock_bin() {
    rm -rf "${MOCK_BIN}"
}

# Create a mock command that echoes fixed output and exits with a given code.
# Usage: mock_cmd <name> <output> [exitcode]
mock_cmd() {
    local name="$1" output="$2" exitcode="${3:-0}"
    printf '#!/bin/bash\nprintf "%%s\n" "%s"\nexit %s\n' "${output}" "${exitcode}" \
        > "${MOCK_BIN}/${name}"
    chmod +x "${MOCK_BIN}/${name}"
}

# Create a mock command that writes fixed output to stdout and stderr, capturing
# any arguments passed to it in a file so tests can inspect them.
# Usage: mock_cmd_capture <name> <output> [exitcode]
mock_cmd_capture() {
    local name="$1" output="$2" exitcode="${3:-0}"
    local call_file="${MOCK_BIN}/${name}.calls"
    printf '#!/bin/bash\nprintf "%%s\n" "%s"\nprintf "%%s\\n" "$*" >> "%s"\nexit %s\n' \
        "${output}" "${call_file}" "${exitcode}" \
        > "${MOCK_BIN}/${name}"
    chmod +x "${MOCK_BIN}/${name}"
}

# Return the captured argument log for a mock_cmd_capture command.
# Usage: mock_calls <name>
mock_calls() {
    local name="$1"
    cat "${MOCK_BIN}/${name}.calls" 2>/dev/null || true
}

# Create a mock command from a multiline script body.
# Usage: mock_cmd_script <name> <body>
mock_cmd_script() {
    local name="$1" body="$2"
    printf '#!/bin/bash\n%s\n' "${body}" > "${MOCK_BIN}/${name}"
    chmod +x "${MOCK_BIN}/${name}"
}
