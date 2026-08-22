#!/usr/bin/env bats
# Unit tests for scripts/install-adguard.sh
# Tests architecture mapping, GitHub release version parsing, tarball URL
# construction, install location, and failure handling. All network and
# archive commands are mocked; AGH_DIR is redirected into a temp dir.

load '../helpers/mock_commands'

SCRIPT_DIR="$( cd "$( dirname "$BATS_TEST_FILENAME" )/../.." && pwd )"
INSTALL_SCRIPT="${SCRIPT_DIR}/scripts/install-adguard.sh"

setup() {
    setup_mock_bin

    export _STATE_DIR
    _STATE_DIR="$(mktemp -d)"
    export AGH_DIR="${_STATE_DIR}/opt/AdGuardHome"

    # Canned GitHub API response; tests may override the version.
    export CURL_API_JSON='{"tag_name": "v0.107.56"}'

    # Mock uname per-test via mock_uname <arch>.
    # Mock curl: log args, serve API JSON on stdout, create a stub file for
    # downloads (honouring -o <path>).
    mock_cmd_script curl 'echo "$*" >> "$MOCK_BIN/curl.calls"
if [[ "$*" == *"api.github.com"* ]]; then
    printf "%s\n" "$CURL_API_JSON"
else
    prev=""
    for a in "$@"; do
        if [[ "$prev" == "-o" ]]; then
            mkdir -p "$(dirname "$a")"
            : > "$a"
        fi
        prev="$a"
    done
fi'
    # Mock tar: extract into the -C destination as AdGuardHome would.
    mock_cmd_script tar 'dest=""
prev=""
for a in "$@"; do
    if [[ "$prev" == "-C" ]]; then dest="$a"; fi
    prev="$a"
done
mkdir -p "$dest/AdGuardHome"
printf "#!/bin/sh\necho adguard-mock-binary\n" > "$dest/AdGuardHome/AdGuardHome"
chmod +x "$dest/AdGuardHome/AdGuardHome"'
}

teardown() {
    teardown_mock_bin
    rm -rf "${_STATE_DIR}"
}

mock_uname() {
    mock_cmd "uname" "$1" 0
}

@test "install-adguard: aarch64 maps to arm64 tarball" {
    mock_uname "aarch64"
    run bash "${INSTALL_SCRIPT}"
    [ "$status" -eq 0 ]
    grep -q "AdGuardHome_linux_arm64.tar.gz" "$MOCK_BIN/curl.calls"
}

@test "install-adguard: armv7l maps to arm tarball" {
    mock_uname "armv7l"
    run bash "${INSTALL_SCRIPT}"
    [ "$status" -eq 0 ]
    grep -q "AdGuardHome_linux_arm.tar.gz" "$MOCK_BIN/curl.calls"
}

@test "install-adguard: unsupported architecture exits 1" {
    mock_uname "x86_64"
    run bash "${INSTALL_SCRIPT}"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Unsupported architecture"* ]]
}

@test "install-adguard: installs executable binary into AGH_DIR" {
    mock_uname "aarch64"
    run bash "${INSTALL_SCRIPT}"
    [ "$status" -eq 0 ]
    [ -x "${AGH_DIR}/AdGuardHome" ]
}

@test "install-adguard: release tag from API is used in download URL" {
    export CURL_API_JSON='{"tag_name": "v9.9.9-test"}'
    mock_uname "aarch64"
    run bash "${INSTALL_SCRIPT}"
    [ "$status" -eq 0 ]
    grep -q "releases/download/v9.9.9-test/AdGuardHome_linux_arm64.tar.gz" \
        "$MOCK_BIN/curl.calls"
    [[ "$output" == *"v9.9.9-test"* ]]
}

@test "install-adguard: queries the GitHub latest-release API exactly once" {
    mock_uname "aarch64"
    run bash "${INSTALL_SCRIPT}"
    [ "$status" -eq 0 ]
    [ "$(grep -c "api.github.com" "$MOCK_BIN/curl.calls")" -eq 1 ]
}

@test "install-adguard: API failure aborts without installing" {
    mock_cmd "curl" "" 1
    mock_uname "aarch64"
    run bash "${INSTALL_SCRIPT}"
    [ "$status" -ne 0 ]
    [ ! -e "${AGH_DIR}/AdGuardHome" ]
}

@test "install-adguard: malformed API response aborts without installing" {
    export CURL_API_JSON='{"unexpected": true}'
    mock_uname "aarch64"
    run bash "${INSTALL_SCRIPT}"
    [ "$status" -ne 0 ]
    [ ! -e "${AGH_DIR}/AdGuardHome" ]
}

@test "install-adguard: reports installed version and path" {
    mock_uname "aarch64"
    run bash "${INSTALL_SCRIPT}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"installed to ${AGH_DIR}"* ]]
}
