# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.6.0] - 2026-05-06

### Fixed — Critical / Security
- `start-bt-tether.sh`: gateway now captured BEFORE `ip route del default dev bnep0`; previously the route was deleted first, causing the capture to always return empty and Bluetooth PAN to never become a routable uplink
- `server.py`: `chpasswd` failure in `_spawn_install` now writes `FAIL_FILE` and exits 1 instead of silently creating `DONE_FILE`; `ROOTPW_FILE` is now always deleted (success or failure)
- `systemd/wan-watchdog.service` + `systemd/failover-watchdog.service`: removed `Restart=on-failure` / `RestartSec=10` — systemd silently ignores `Restart=` on `Type=oneshot` services; the round-6 addition had no effect

### Fixed — Reliability / Correctness
- `travel-router-firewall.sh`: `ENABLE_PER_DEVICE_VPN=0` now tears down the `VPN_DEVICES` mangle chain and `ip rule fwmark 0x64 lookup 100`; previously stale entries persisted routing marked traffic via Tailscale indefinitely
- `notify-router.sh`: `PRIORITY` parameter validated against known ntfy.sh values; unrecognised values fall back to `"default"` preventing HTTP header injection
- `start-bt-tether.sh`: `BT_MAC` validated as `([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}` before use
- `failover-watchdog.sh`: `set_default_metric` now uses `ip route replace` (atomic) instead of `del`+`add`, eliminating the routing gap window; `_UPLINK_STATE_FILE` written atomically via mktemp+mv
- `wan-watchdog.sh`: `STATE_FILE` (fail counter) written atomically via mktemp+mv
- `tailscale-watchdog.sh`: corrupt `ts-peers.json` now detected via `jq empty` and reset to `[]` baseline instead of silently discarding peer-loss events; lost peers now notified one per call instead of concatenated
- `ups-monitor.sh`: numeric guard added for `LEVEL` from `_get_battery_pct`; non-numeric sysfs content no longer causes arithmetic failure under `set -e`
- `stop-bt-tether.sh`: `ip route del default dev bnep0` added before `ip link set down` to prevent stale kernel routes
- `ap-schedule.sh`: `enable` branch now calls `_wait_hostapd` before issuing `hostapd_cli enable`, matching the `disable` branch
- `travel-router-firewall.sh`: `grep` pattern for `fwmark 0x64 lookup 100` anchored to prevent false positives on `lookup 1000` etc.
- `server.py`: `err_preseed` JSON on validation failure now built from an explicit allowlist; `TS_KEY` and `TOR_AP_PASS` are no longer included
- `server.py`: `_spawn_install()` wrapped in `try/except OSError`; temp files cleaned up and 500 returned on Popen failure
- `server.py`: `SPLIT_TUNNEL_DOMAINS` regex tightened to RFC 952 — rejects leading dots, leading/trailing hyphens per label
- `server.py`: `TAILSCALE_UP_ARGS` forbidden-flag check uses exact-match/`=`-prefix to avoid false positives
- `install.sh`: `ROUTER_HOSTNAME` regex updated to `[a-zA-Z0-9-]{0,61}` enforcing the 63-char RFC 952 label limit
- `install.sh`: `AP_PASS` / `TOR_AP_PASS` now reject `#` characters in all install paths
- `update-router.sh`: `chmod 755` applied to `${dest}.tmp` BEFORE `mv`, eliminating the window where a live script is non-executable; `tar` extraction adds `--no-absolute-names`; `VERSION_FILE` writes atomic via tmp+mv; `log()` uses `printf` not `echo`
- `update-blocklists.sh`: `NFT_NEW` created via `mktemp` and added to EXIT trap; no stale partial file left on failure
- `travel-tui.sh`: `uplink.state` value and `_bw_delta` iface parameter validated against `^[a-zA-Z0-9_.-]{1,15}$` before use in file paths; `_ap_edit_ssid` rejects SSID > 32 chars or containing control characters; Settings SSH key validated for key-type prefix; `tput reset` added after `bmon`/`iftop` to restore terminal state
- `travel-diagnostic.sh`: output directory validated as existent and writable before collection begins
- `build/stage-travel-router/files/imager-compat.sh`: `firstrun.sh` stub written atomically via mktemp+mv

### Fixed — Configuration / Systemd
- `config/AdGuardHome.yaml`: `anonymize_client_ip: true` — default config no longer logs full client IPs for all DNS queries
- `config/91-android-tether.rules`: `add` rules set `TAG+="pi_android_tether"`; `remove` rules match `TAGS==` instead of `ENV{ID_VENDOR_ID}` (unreliable at removal time)
- `config/hostapd.conf`: hardcoded `country_code=US` removed; operators must set the correct country code for their jurisdiction
- `systemd/generate-bandwidth-report.service`: `Wants=vnstat.service` added alongside existing `After=`
- `firstboot/firstboot.service`: `ProtectHome=yes` and `PrivateDevices=yes` added
- `systemd/ap-disable.service` + `ap-enable.service`: `After=NetworkManager.service network.target` added
- `systemd/wan-watchdog.timer` + `failover-watchdog.timer`: `After=<service>` added for correct ordering
- `systemd/tune-cake.timer`, `daily-digest.timer`, `generate-bandwidth-report.timer`, `vnstat-push.timer`: `RandomizedDelaySec=15min` added to prevent thundering-herd on multi-router deployments

## [1.5.0] - 2026-05-06

### Fixed — Critical / Security
- `travel-router-firewall.sh`: policy order corrected — `iptables -P FORWARD DROP` now set *before* `iptables -F FORWARD`; on the very first invocation (fresh boot with ACCEPT baseline) all packets were previously forwarded unfiltered during the flush window
- `update-router.sh`: `readonly REPO` (and other critical variables) added before sourcing `/etc/default/travel-router`; a compromised config file could previously override `REPO` and redirect updates to an attacker-controlled GitHub repository
- `install.sh`: WiFi QR code assembly now validates `AP_SSID`/`AP_PASS` for shell-unsafe characters (`\``, `$`, `(`, `)`) before construction; backtick/subshell injection in the MECARD string is no longer possible
- `build/stage-travel-router/01-run.sh` + `firstboot/firstboot.service`: `ExecStartPost` drop-in added to shred `/boot/firmware/root-password.txt` after firstboot completes; the FAT32 boot partition has no Unix permission enforcement and the plaintext password was readable by anyone with physical access to the SD card
- `config/AdGuardHome.yaml`: admin UI bound to `127.0.0.1:3000` instead of `0.0.0.0:3000`; previously reachable by anyone on the hotel network before a password was set

### Fixed — Reliability / Correctness
- `captive-check.sh`: `tailscale down 2>/dev/null || true` — non-zero exit from tailscale (daemon not running, already disconnected) no longer aborts the script under `set -e`, which previously left a stale portal-active state file and permanently disabled auto-login retries
- `tailscale-watchdog.sh`: `flock -n 9 || exit 0` guard added; concurrent invocations from rapid timer fires no longer race on state file writes
- `start-bt-tether.sh`: `bt-pan` PID captured and cleaned up via EXIT trap if setup fails; orphaned bt-pan processes no longer accumulate on repeated udev events
- `ups-monitor.sh`: `UPS_SHUTDOWN_THRESHOLD` now validated as integer with fallback warning; non-numeric config values no longer cause silent shutdown failure
- `notify-router.sh`: dead `python3` guard removed; the guard blocked all notifications on systems without python3 even though python3 is not used
- `wan-watchdog.sh`: `WAN_PING_TARGETS` now read into array to prevent glob expansion; `captive-check.sh` non-zero exit now logged
- `start-tether.sh` / `stop-tether.sh`: interface name validated against `^(enx[0-9a-f]+|rndis0|usb0)$` before use
- `ap-schedule.sh`: `_wait_hostapd` now probes with `hostapd_cli ping | grep PONG` instead of checking socket existence; stale socket files from a crashed hostapd no longer produce false-ready indications
- `generate-bandwidth-report.sh`: interface names and `$(date)` output HTML-escaped before embedding in report headings
- `vnstat-push.sh`: active interface from `uplink.state` validated against `^[a-zA-Z0-9_.-]{1,15}$` before appending to Pushgateway URL path
- `update-router.sh`: version string from GitHub API sanitized with `tr -cd 'A-Za-z0-9._-'`; `log()` uses `printf` instead of `echo` to avoid escape-sequence interpretation
- `update-blocklists.sh`: `curl` call now uses `--fail`; HTTP error responses (404/503) no longer silently treated as valid blocklist data
- `tune-cake.sh`: CAKE state file written atomically via tmp+mv
- `vnstat-metrics.sh`: Prometheus metrics file written atomically; node-exporter no longer sees truncated scrape files on parser failure
- `travel-tui.sh`: cleanup trap extended to `EXIT` so cursor is always restored; `read` calls guarded with `|| true`; MECARD QR escaping added for `;`, `,`, `"`, `\`, `:`
- `captive-check.sh`: `_probe()` temp file cleaned up on `RETURN` trap, preventing tmpfs accumulation

### Fixed — Configuration / Systemd
- `config/sshd-travel-router.conf`: `PasswordAuthentication no` added; installs via `install.sh` (not the pre-built image) previously left password SSH login enabled
- `config/avahi-daemon.conf`: `enable-wide-area=no`; previously leaked mDNS service names to hotel/upstream DNS infrastructure
- `config/91-android-tether.rules`: `ACTION=="remove"` rules now use `ENV{ID_VENDOR_ID}` instead of `ATTRS{idVendor}`; sysfs device attributes are often unavailable at removal time
- `config/nftables-travel-router.nft`: `ip6 dscp set cs0` rules added for uplink interfaces; IPv6 hotspot traffic was previously not DSCP-stripped, enabling carrier fingerprinting
- `systemd/adguard-home.service`: `NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectSystem=strict` added; AdGuard Home processes untrusted DNS queries and was running as root with no sandbox
- `systemd/failover-watchdog.service` + `systemd/wan-watchdog.service`: `Restart=on-failure` / `RestartSec=10` added; a single script error previously silently skipped the watchdog cycle
- `systemd/daily-digest.service`: `Wants=network-online.target` added alongside `After=` so systemd pulls the target into the transaction
- `firstboot/server.py`: `SPLIT_TUNNEL_DOMAINS` regex anchored and single-space-separated (no double-spaces producing `//`); `TAILSCALE_UP_ARGS` blocks `--auth`/`--reset`/`--force-reauth` flags; `/setup` Content-Length reduced to 32 KB, `/retry` to 8 KB
- `install.sh`: `wpa_supplicant.conf` now chmod 600 immediately after write; `ROUTER_TIMEZONE` validated before `timedatectl` call; `PasswordAuthentication no` write guarded with `grep -q` to prevent duplicates

## [1.4.0] - 2026-05-06

### Fixed — Critical / Security
- `server.py` / `install.sh`: `SSH_ADMIN_KEY` newline injection — multi-line values could inject extra keys into `/root/.ssh/authorized_keys`; both now strip `\n`/`\r` and validate the key starts with `ssh-`, `ecdsa-`, or `sk-`
- `travel-router-firewall.sh`: ERR trap added — if any `iptables` command fails under `set -e`, policy is restored to `FORWARD DROP` and the chain is flushed instead of leaving the firewall wide open with `ACCEPT` policy and no rules

### Fixed — Reliability / Correctness
- `stop-tether.sh`: `wan-watchdog.service` is now restarted (`systemctl --no-block restart`) after tether teardown so the watchdog re-evaluates uplinks immediately instead of routing through the dead interface
- `start-bt-tether.sh`: gateway capture now retries up to 5 times (1 s apart) before giving up; previously the route was queried before the interface was fully up
- `ups-monitor.sh`: `0%` battery is now a valid charge level — the `[[ pct -gt 0 ]]` guard that rejected it has been replaced with a `0–100` range check
- `tailscale-watchdog.sh`: removed `head -1` from stale-peer pipeline; all stale peers are now disconnected in one pass instead of only the first
- `install.sh`: `AP_PASS`/`TOR_AP_PASS` newlines stripped in non-interactive CLI path; previously only interactive input was sanitised
- `install.sh`: `_BARE_IP_RE` tightened to reject octets > 255 (e.g. `999.x.x.x`)
- `install.sh`: `ROUTER_HOSTNAME` regex updated to `^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$` — trailing hyphens (RFC 952 invalid) are now rejected
- `install.sh`: `_read_ap_ssid` now uses `shlex.split()` to re-parse the stored value; single quotes in SSIDs no longer garble the result
- `travel-tui.sh`: toggling `ENABLE_SPLIT_TUNNEL` / `ENABLE_BANDWIDTH_DASHBOARD` now calls `systemctl try-restart` / `systemctl stop` on the corresponding service so the change takes effect immediately without a reboot
- `travel-tui.sh`: large byte-counter arithmetic ported to `awk`; bash `$(( ))` overflow on 64-bit-signed-max values in GB range eliminated
- `update-router.sh`: `shopt -s nullglob` guards added around all glob-based `for` loops; a missing match no longer iterates over a literal glob string

### Fixed — Low / Housekeeping
- `ap-schedule.sh`: `_wait_hostapd()` helper added; `hostapd_cli` calls now wait up to 10 s for the control socket before acting
- `start-tether.sh` / `stop-bt-tether.sh`: `systemd-run --unit` names now include `$$` suffix to prevent name-collision errors when called in rapid succession
- `notify-router.sh`: `python3` presence now checked at startup; exits cleanly with a log message if not installed
- `ups-monitor.sh`: dead `&& pct -gt 0` condition removed from REST API path
- `travel-tui.sh`: UUOC — `cat uplink.state` replaced with `read -r … < uplink.state`
- `config/91-android-tether.rules`: added `ATTRS{idVendor}!="1d6b"` to exclude the Pi's own Linux Foundation USB gadget interface from triggering `start-tether.sh` on every boot
- `.github/workflows/shellcheck.yml`: permanently no-op "Shellcheck firstboot scripts" step removed (no `.sh` files exist in `firstboot/`)
- `.github/workflows/build-image.yml`: `if: always()` cleanup step added to unmount and remove leftover `MOUNTPOINT` temp directories on failure
- `README.md`: SSH section rewritten — accurately describes key-only authentication, how to add an SSH key, that password login is disabled by design, and that `/boot/firmware/root-password.txt` is for console access only

## [1.3.0] - 2026-05-07

### Fixed — Critical
- `apply-split-tunnel.sh`: fwmark mismatch in `teardown_split_tunnel` — teardown used decimal `1` while setup used hex `0x2`; routing rule persisted forever after disabling split-tunnel

### Fixed — Security / Correctness
- `captive-check.sh` `attempt_portal_login`: `ssid_slug` now strips `.` from allowed chars so an SSID named `..` can no longer produce path-traversal segments into parent directories
- `captive-check.sh` `_probe()`: `redirect_url` validated as `http*` before use; HTTP status code string can no longer be passed as a URL
- `server.py` `/setup`: double-submission guard — browser back+resubmit after install starts now redirects to `/status` instead of spawning a second concurrent `install.sh`
- `server.py` `_validate()`: `SPLIT_TUNNEL_DOMAINS` now validated with regex; shell-unsafe characters rejected at form submission time
- `server.py`: root password null-byte (`\x00`) check added — `chpasswd` is a C program that reads until null terminator, so a null in the validated password would silently shorten it
- `install.sh` interactive path: control-character guard added for `AP_SSID` and `AP_PASS` entered at the prompt
- `install.sh`: `ROUTER_HOSTNAME` validated against `^[a-z0-9][a-z0-9-]{0,62}$` before use in `sed` (direct-run path had no validation)
- `build/config`: `FIRST_USER_PASS` changed from plaintext `changeme` to a placeholder; `01-run.sh` now randomises the pi user's password before deletion

### Fixed — Reliability
- `notify-router.sh`: `curl` now ends with `|| true`; transport errors no longer propagate to callers under `set -euo pipefail`
- `start-tether.sh`: `notify-router.sh` call guarded with `|| true`; ntfy unreachability can no longer fail the tether service
- `start-bt-tether.sh`: `ip route add` for bnep0 guarded with `|| true`; a pre-existing route no longer short-circuits CAKE setup and notification
- `tailscale-watchdog.sh`: all 4 `jq` assignments restructured to `if ! var=$(jq ...)` — the previous `$?` guards were dead code under `set -euo pipefail` and would never execute on parse failure
- `ups-monitor.sh`: `THRESHOLD` validated as numeric immediately after loading from config; non-numeric value falls back to 10 with a warning instead of silently becoming 0
- `failover-watchdog.sh` `set_default_metric`: `ip route add` failure now logged instead of silently swallowed
- `wan-watchdog.sh` `truncate_log`: SC2015 `A && B || C` anti-pattern replaced with `if/else`; `mv` failure no longer triggers log data deletion
- `travel-tui.sh` `_cfg_edit`: Python inline now uses `shlex.quote()` to write config values; double-quotes in user-entered values no longer produce malformed shell syntax
- `travel-tui.sh` client table: `printf '%*s'` width capped at zero to prevent misalignment on hostnames/IPs longer than the column width

### Fixed — Monitoring / Observability
- `travel-diagnostic.sh`: secret redaction pattern changed to match to end-of-line; values containing spaces were previously only partially redacted
- `update-blocklists.sh`: `TMP_FILE` now uses `mktemp` with `trap … EXIT` for cleanup; fixed path eliminated TOCTOU symlink risk
- `update-router.sh`: tmpdir trap extended to `EXIT INT TERM`; portal scripts installation now checks against an explicit `PORTAL_ALLOWLIST`
- `vnstat-metrics.sh`: `TIMESTAMP` passed as `sys.argv[1]` instead of interpolated into Python heredoc; heredoc/pipe stdin conflict resolved

### Build / CI / Systemd
- `.github/workflows/shellcheck.yml`: `paths` filter moved off the tag trigger so shellcheck always runs when a release tag is pushed
- `systemd/adguard-home.service`: obsolete `syslog.target` dependency removed; `StandardOutput/StandardError=journal` added
- `systemd/generate-bandwidth-report.service`: `StandardOutput/StandardError=journal` added (consistent with peer services)
- `.github/workflows/build-image.yml`: `trap … EXIT` added to smoke-test and SBOM steps to clean up loop devices on failure

## [1.2.0] - 2026-05-07

### Fixed — Critical
- `firstboot.service`: removed `ProtectSystem=strict` + `ProtectHome=yes` which made `/etc`, `/usr`, and `/boot` read-only in the spawn namespace, causing every `install.sh` write to fail with EROFS; sandbox directives were incompatible with the child process that legitimately needs full filesystem access
- `wan-watchdog.sh` `truncate_log`: replaced fixed `.tmp` path with `mktemp` to eliminate race condition / zero-length log on crash

### Fixed — Security / Correctness
- `server.py`: `AP_SSID` now rejects control characters (embedded `\n` via crafted POST would have injected a new line into `hostapd.conf`)
- `server.py`: `AP_PASS` and `TOR_AP_PASS` now reject `#` (`hostapd.conf` treats `#` as comment start, silently truncating the passphrase)
- `install.sh`: WiFi QR string now escapes `\`, `;`, `,`, `:`, `"` per ZXing WPA spec so phone cameras can parse the code when SSID/pass contain these characters
- `install.sh` `_safe_write_conf`: switched from double-quoted `KEY="val"` to `shlex.quote()` (single-quoted) to prevent `$var` expansion when `/etc/default/travel-router` is sourced
- `build/stage-travel-router/01-run.sh`: `PasswordAuthentication` changed to `no`; the image comment already stated "key only" but the directive contradicted it
- `failover-watchdog.sh`: inline `ip route change` replaced with `set_default_metric` function call; `ip route change` fails silently when the route changed between check and action
- `captive-check.sh`: added `set -euo pipefail`; added `base_url` guard in `attempt_portal_login`; replaced fragile nested `eval "$_prev_trap"` trap restore with direct inline cookie jar cleanup
- `travel-router-firewall.sh`: `iptables/ip6tables -P FORWARD ACCEPT` now set immediately after flush so no packets are dropped during the rebuild window
- `start-bt-tether.sh` / `stop-bt-tether.sh`: `failover-watchdog.sh` now dispatched via `systemd-run --no-block` (same fix as `start-tether.sh`) to avoid blocking udev worker threads
- `update-router.sh`: `stop-tether.sh` and `vnstat-metrics.sh` added to `SCRIPT_ALLOWLIST` so they receive updates during auto-update runs

### Fixed — Reliability
- `install.sh`: `PUSHGW_URL`, `UPS_SHUTDOWN_THRESHOLD`, and `TAILSCALE_UP_ARGS` now persisted to `/etc/default/travel-router` via `_safe_write_conf` so wizard values survive reboot
- `firstboot.service`: added `Wants=imager-compat.service` alongside `After=` so systemd starts the compat shim when it exists
- `wan-watchdog.sh` recovery step 3: added `nmcli device connect wlan0` after `ip link set wlan0 up` to re-trigger STA association
- `failover-watchdog.sh` `set_default_metric`: skip `ip route add` if gateway is empty to prevent gateway-less default routes
- `apply-split-tunnel.sh` `teardown_split_tunnel`: delete the `iptables mangle PREROUTING` mark rule on teardown to prevent rule accumulation
- `ups-monitor.sh`: numeric regex guard before `-gt` comparison to avoid crash on non-integer API response under `set -euo pipefail`
- `notify-router.sh`: `${NTFY_TOPIC:-}` instead of `$NTFY_TOPIC` to avoid `set -u` unbound variable error
- `ap-schedule.sh`: both `hostapd_cli` calls now include `-i uap0` to target the correct VAP
- `tailscale-watchdog.sh`: `jq` parse errors restructured to call `exit 1` in the outer shell (not a no-op subshell)
- `generate-bandwidth-report.sh`: `logger` call fixed to avoid literal `%s` in syslog output
- `update-blocklists.sh`: blank lines excluded from `COUNT` so the sanity guard accurately reflects real IP entries
- `travel-tui.sh` `show_features`: `sed -i` replaced with inline Python atomic writer to safely handle `/`, `&` in flag values
- `vnstat-push.sh`, `tune-cake.sh`: useless `cat | tr` pipeline replaced with input redirection

### Build / CI
- `.github/workflows/build-image.yml`: `systemd/**` and `config/**` added to push/PR path triggers
- `build/stage-travel-router/01-run.sh`: removed redundant `-e` from shebang (superseded by `set -euo pipefail`)
- `config/travel-router-defaults`: added `AP_SUBNET` and `AP_GATEWAY` defaults for consistent reference across scripts

## [1.1.0] - 2026-05-06

### Fixed — Security (17 fixes)
- Image: random root password written to `/boot/firmware/root-password.txt`; `PermitRootLogin prohibit-password` in image build
- Wizard (`server.py`): CSRF token on `/retry`; preseed XSS fix (`</` → `<\/`); Host header allowlist (DNS rebinding prevention); SSID byte-length validation; AP_PASS printable-ASCII enforcement; UPS threshold 1–99 range check; PUSHGW_URL URL validation; 512-char cap on TAILSCALE_UP_ARGS; double-submit redirects to `/status`; log tail capped at 512 KB
- `travel-router-firewall.sh`: IPv6 FORWARD chain DROP policy; SSH blocked from `wlan0`; Prometheus (9100) and AdGuard (3000) blocked from `uap0`
- `install.sh`: log written mode 0600; BBR moved to `/etc/sysctl.d/99-bbr.conf`; `TAILSCALE_UP_ARGS` uses `read -ra`; WiFi QR redirected to `/dev/tty`; `PermitRootLogin prohibit-password` set unconditionally; open-WiFi fallback auto-enables kill switch

### Fixed — Reliability (25 fixes)
- `failover-watchdog.sh`: uplink state file always written on first active uplink; failed interfaces demoted to metric 900; DORMANT/UNKNOWN interface states detected; gateway read at action time not snapshot; route-del guarded by existence check; atomic log truncation via `mktemp`; `_notify_safe()` wrapper with logger fallback
- `wan-watchdog.sh`: dual HTTPS probe before declaring WAN down; `sleep 4` between disconnect/reconnect; confirmed `hostapd` stopped before `wlan0` down
- `captive-check.sh`: STATE_FILE moved to `/var/lib/travel-router/`; Tailscale restored + state file removed immediately after successful portal login; curl exit code checked; outer EXIT trap saved/restored in `attempt_portal_login`; `read -ra` for TAILSCALE_UP_ARGS
- `apply-split-tunnel.sh`: ipset teardown name fixed (`vpn_domains` consistent); `tailscale0` absence logged before route add
- `start-tether.sh`: failover-watchdog dispatched via `systemd-run --no-block`; UNKNOWN state accepted in poll; `TC_TETHER_BW` configurable (default 50mbit)
- `start-bt-tether.sh`: polls for `inet` address before proceeding; bt-pan liveness check after 2 s
- `stop-bt-tether.sh`: `nmcli disconnect` before `ip link down`; stale default route removed
- `clone-mac.sh`: hostapd stopped/restarted around MAC change; successful clone saved to `/var/lib/travel-router/cloned-mac`
- `ap-schedule.sh`: `enable` starts hostapd if not running; control socket checked before `hostapd_cli`
- `tailscale-watchdog.sh`: flock guard; configurable `TS_STALE_HANDSHAKE_SECS`; only alerts on peers with `TxBytes > 0`; exits 1 on daemon unreachable
- `ups-monitor.sh`: API response validated as integer 0–100; notify wrapped in `timeout 10`; shutdown hysteresis flag

### Fixed — TUI & Monitoring (15 fixes)
- `travel-tui.sh`: `if/else` replaces `&& B || C` for service toggle; `/run/travel-router/` for temp files; atomic `_cfg_edit` via `os.replace()`; SSID/passphrase edits use Python rewrite; `read` timeout vs closed-stdin distinguished; `chpasswd <<<` replaces `printf | chpasswd`; 32-bit counter wrap detection; `_cleanup clear` only on normal exit
- `travel-status.sh`: CPU from `/proc/stat` two-sample; `AP_IFACE` variable respected
- `travel-diagnostic.sh`: EXIT trap for temp-dir cleanup; stderr captured in `collect`; `HEADSCALE_URL`, `TOR_AP_PASS`, `PUSHGW_URL`, `IPHONE_BT_MAC` redacted
- `generate-bandwidth-report.sh`: atomic write via `.tmp`; HTML-escaped vnstat output
- `vnstat-push.sh`: single Python call per interface; active uplink included; PUSHGW_URL credentials redacted in logs
- `tune-cake.sh`: CAKE applied to `$UPLINK_IFACE` not hardcoded `wlan0`; `LC_ALL=C` for speedtest
- `daily-digest.sh`: `vnstat --json` + Python replaces `--oneline`; `AP_IFACE` variable
- `update-router.sh`: explicit script-name allowlist for `/usr/local/bin/` installs
- `update-blocklists.sh`: abort if blocklist < 100 entries; atomic rollback to `.prev` on `nft` failure

### Fixed — Build & CI (12 fixes)
- `00-packages`: pre-seeded 17 packages (`hostapd`, `dnsmasq`, `iptables*`, `jq`, `usbmuxd`, `libimobiledevice*`, `macchanger`, `vnstat`, `iw`, `qrencode`, `nftables`, `wireless-tools`, `bmon`)
- `build/config`: `WPA_COUNTRY=''` (install.sh is sole authority)
- `build-image.yml`: `softprops/action-gh-release` pinned to commit SHA; PR path trigger; image smoke-test step; SBOM generation; `workflow_dispatch` `git_ref` input; extended apt cache key
- `shellcheck.yml`: `ludeeus/action-shellcheck` pinned to commit SHA; `push: tags: v*` trigger
- `python-lint.yml`: switched from `pyflakes` to `flake8` + `pylint`
- `.github/dependabot.yml`: weekly github-actions dependency updates

### Fixed — Documentation (13 fixes)
- `build/stage-travel-router/01-run.sh`: random root password; `PermitRootLogin prohibit-password`; pi-user deletion assertion
- `build/README.md`: correct QEMU symlink commands
- `README.md`: Android tether metric corrected; RaspAP credential change warning
- `AGENTS.md`: nftables mangle attribution; `rndis0` interface row; `firstboot/` and `build/` in structure table
- `CHANGELOG.md`: duplicate entries removed; version comparison footer links
- `.github/CONTRIBUTING.md`: quad-core CPU fix; issue template creation URLs; dev deps note
- `CODE_OF_CONDUCT.md`: expanded from stub to proper conduct document
- `pull_request_template.md`: TUI coverage checklist item
- `.github/SUPPORT.md`: created
- `firstboot/README.md`: security note about unauthenticated `/status` log

### Added
- `firstboot/firstboot.service`: systemd sandbox (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`, capability bounding)
- `notify-router.sh`: `NTFY_TOKEN` bearer auth support; `set -euo pipefail`
- `wizard (index.html)`: 250-word passphrase list (5 words, ~39 bits); 12 new country options; inline mobile-friendly confirm; empty TAILSCALE_UP_ARGS default

## [1.0.0] - 2026-05-06

### Fixed — Security & Correctness (27 fixes)
- `captive-check.sh`: STATE_FILE touch moved before `tailscale down`; cookie jar uses `mktemp` with EXIT trap; SSID slug sanitised to `[a-zA-Z0-9._-]` capped at 64 chars; form `action=` extraction handles single-quoted and bare attributes; `|| true` on portal script source
- `travel-router-firewall.sh`: `iptables -P FORWARD DROP` moved after all ACCEPT rules to eliminate traffic blackhole window during startup
- `failover-watchdog.sh`: flock guard prevents overlapping runs; `get_gateway`/`get_metric` use awk string equality; boot-notification suppressed when no previous uplink; ICMP ping replaced with dual HTTP probe (gstatic + detectportal)
- `firstboot/server.py`: 409 on double-submit; `/retry` endpoint; CSRF token; `X-Frame-Options`, `X-Content-Type-Options`, `Content-Security-Policy` headers; content-type validation (415); strict UTF-8 decode (400 on error); hostname and time regex hardened; headscale URL validated; ntfy topic capped at 64 chars; spawn exit code captured explicitly
- `install.sh`: hostapd.conf `ssid=` and `wpa_passphrase=` written via Python (safe for `#` and spaces); all config substitutions use `_safe_write_conf` Python helper; Tor passphrase guard; hostname substitution via Python
- `apply-split-tunnel.sh`: `modprobe ip_set` guard; `teardown_split_tunnel()` when disabled
- `update-router.sh`: atomic `mv` for all script writes; tarball integrity check via `tar -tjf`; portal examples synced
- `build/stage-travel-router/01-run.sh`: `git clone --depth=50` with 5-attempt retry; `dtoverlay=dwc2` inserted under existing `[all]`; `imager-compat.sh` extracted from heredoc to `files/`

### Fixed — Hard Reliability (18 fixes)
- `travel-tui.sh`: `_cfg_edit` rewritten with Python heredoc (safe for `|`, `\`, newlines); `show_clients` converted from tail-recursion to `while true` loop; re-source config after SSH key edit
- `failover-watchdog.sh`: HTTP probe for internet reachability
- `notify-router.sh`: `--max-time 10`; NTFY_TOPIC URL-encoded via `urllib.parse.quote`
- `start-bt-tether.sh`: bt-pan PID saved to `/run/bt-pan.pid`; duplicate-start guard; `nmcli` DHCP with `dhcpcd` fallback
- `clone-mac.sh`: `--restore` uses `macchanger -p` (permanent hardware MAC, not random)
- `update-blocklists.sh`: `mv` before `nft -f` for correct persistence order
- `tailscale-watchdog.sh`: `command -v jq` guard
- `ups-monitor.sh`: sleep before shutdown increased to 20 s
- `stop-bt-tether.sh`: `set -euo pipefail`; calls `failover-watchdog.sh` on exit
- `build/stage-travel-router/00-packages`: added `python3`, `network-manager`
- `firstboot/firstboot.service`: `After=network-online.target`; `TimeoutStartSec=infinity`; `RequiresMountsFor=/opt`

### Fixed — Medium Priority (25 fixes)
- `travel-tui.sh`: dynamic flag index bounds check; `_bw_delta` uses elapsed time from timestamp file; WiFi password masked in QR flow; batched `systemctl is-active`; `AP_IFACE` variable; `_cpu_usage` reads `/proc/stat` twice
- `failover-watchdog.sh`: `_notify_uplink_change` requires non-empty previous uplink; `truncate_log` helper
- `wan-watchdog.sh`: STATE_FILE moved to `/var/lib/travel-router/`; captive-check only on wlan0; recovery restarts hostapd; `truncate_log` helper
- `firstboot/server.py`: `_load_preseed` sets `ROUTER_HOSTNAME`; `UPS_SHUTDOWN_THRESHOLD`/`PUSHGW_URL`/`TAILSCALE_UP_ARGS` in STRING_FIELDS; exceptions logged with traceback
- `firstboot/index.html`: `crypto.getRandomValues()` passphrase; country selector with 28 destinations; new advanced fields
- `install.sh`: AP schedule timer drop-ins interpolate `$AP_DISABLE_TIME`/`$AP_ENABLE_TIME`; CAKE service install; all new config keys persisted
- `generate-bandwidth-report.sh`: `shopt -s nullglob`; all tether interfaces reported (no early break)
- `start-tether.sh`: polling loop for `state UP` replaces `sleep 3`
- `ap-schedule.sh`: `systemctl is-active --quiet hostapd` guard
- `setup-2fa.sh`: TOTP secret print wrapped in `[ -t 1 ]` TTY check
- `tune-cake.sh`: reads uplink state file; passes `--interface` to speedtest-cli
- `travel-diagnostic.sh`: case-insensitive redaction; collects `/etc/travel-router-version`
- `vnstat-push.sh`: awk JSON replaced with Python

### Fixed — Low Priority / CI (14 fixes)
- `captive-check.sh`: hardened SSID slug
- `stop-bt-tether.sh`: strict mode
- `portals/example-credentials.sh`: `jq` dependency check with Python alternative note
- `log2ram` JOURNALD_AWARE update made idempotent
- `firstboot/server.py`: CSRF token validation; security headers
- `.github/workflows/shellcheck.yml`: pinned to `@2.0.0`; `continue-on-error` removed; firstboot step added
- `.github/workflows/python-lint.yml`: glob for `.py` files
- `build/stage-travel-router/files/imager-compat.sh`: extracted to standalone file for CI syntax checking
- `CODE_OF_CONDUCT.md`: added
- `CONTRIBUTING.md`: updated

## [0.9.1] - 2026-05-06

### Fixed
- Captive-portal failover-watchdog now uses a dual-endpoint HTTP probe instead of ICMP ping for reliable detection behind hotel NAT
- Wizard now includes fields for Bluetooth MAC address, bandwidth cap, and AP schedule times
- Wizard displays a weak-password warning when the AP passphrase is too short or common
- Per-SSID captive-portal script templates: drop a `.sh` named after the hotel SSID into `/etc/travel-router/portals/` for automatic re-authentication
- `travel-status` and TUI dashboard now display Wi-Fi RSSI when the active uplink is hotel/open WiFi
- TUI dashboard now shows AP client IP addresses alongside client count

## [0.9.0] - 2026-05-06

### Added
- Community health files: CONTRIBUTING, SECURITY policy, bug-report and feature-request issue templates, PR template
- Wi-Fi country selector added to the first-boot wizard
- CI build badge added to README
- Windows 10/11 CDC NCM USB note: inbox driver, no installation needed
- GitHub repository topics and description updated

## [0.8.3] - 2026-05-06

### Fixed
- Raspberry Pi Imager compatibility hardening: robust fingerprint detection, improved SSH key extraction, corrected file permissions
- USB gadget switched from `g_ether` to `g_ncm` for Windows 10/11 plug-and-play (inbox CDC NCM driver, no installation needed)
- CHANGELOG versioned: `[Unreleased]` split into `[0.8.0]`, `[0.8.1]`, and `[0.8.2]` sections

## [0.8.2] - 2026-05-06

### Fixed
- USB gadget enumeration: added `modules-load=dwc2,g_ncm` to `cmdline.txt` so the gadget interface comes up reliably on first boot
- Raspberry Pi Imager compatibility: neutralise `firstrun.sh` after consuming it and apply the SSH public key to the root account

## [0.8.1] - 2026-05-06

### Fixed
- CI image build: pinned pi-gen to `bookworm-arm64` branch to match `RELEASE=bookworm`
- CI image build: install `qemu-user-binfmt` alongside `qemu-user` (resolves package conflict with `qemu-user-static`)
- CI image build: add `qemu-arm` symlink for pi-gen binfmt check

## [0.8.0] - 2026-05-05

### Added
- Pre-built SD card image with GitHub Actions build pipeline (pi-gen based)
- First-boot web wizard served at `travelrouter.local` for guided initial setup
- Non-interactive install mode via `INSTALL_NONINTERACTIVE=1` env var
- Root-as-default-user with `changeme` default password (image-only)
- USB gadget mode pre-enabled in image so wizard is reachable via USB-C at `192.168.7.1` before install runs
- `travel-diagnostic` command: collects redacted logs, network state, and config into a timestamped tar.gz
- Wizard error recovery: install failures show last 50 log lines and a Retry button
- AP passphrase generator button in wizard (4-word random passphrase, no external dependencies)
- Raspberry Pi Imager pre-seed compatibility: wizard pre-fills hostname and SSH key from `firstrun.sh` if present

### Changed
- Image builds ship root-only login flow; password change enforced on first wizard submission
- `wan-watchdog.sh` recovery steps now use NetworkManager instead of the removed `dhcpcd`
- `update-router.sh` re-applies firewall rules when `travel-router-firewall.sh` changes in an update

## [0.7.0] - 2026-05-04

### Added
- nftables TTL/DSCP rule migration replacing legacy iptables shims (#1)
- Domain-based split tunnel for selective Tailscale routing (#19)
- SSH two-factor authentication (TOTP) (#27)
- WAN metric auto-management via NetworkManager dispatcher (#32)
- Bandwidth dashboard, Prometheus exporter, and vnStat push integration (#33 #34)
- Real-time traffic inspector built on bmon + iftop (#45)
- PiSugar 3 UPS monitor with battery telemetry (#48)
- Headscale self-hosted control server support (#46)
- Enhanced TUI dashboard with usability improvements

### Changed
- `ENABLE_WAN_METRICS` now defaults to `1` on fresh installs

### Security
- Stateful FORWARD policy with explicit KILL_SWITCH ordering
- CAKE bandwidth auto-tuning
- SSH hardening with optional pubkey-only authentication

## [0.6.0] - 2026-05-04

### Added
- Hardware watchdog, log rotation, daily digest reports, expanded README (#reliability)

## [0.5.0] - 2026-05-04

### Added
- Per-client bandwidth fairness queues
- Per-device Tailscale exit-node routing (#21 #44)

## [0.4.0] - 2026-05-04

### Added
- TUI dashboard, `status` command, MOTD branding
- Uplink alerts, AP schedule, Wi-Fi QR helper

## [0.3.0] - 2026-05-04

### Added
- Avahi reflector for cross-segment mDNS
- Tailscale watchdog
- Android USB tethering support
- AdGuard Home integration (#18 #24 #28 #35)

## [0.2.0] - 2026-05-04

### Added
- DNS-over-TLS upstream resolver
- VPN kill switch
- Unattended security updates (#16 #17 #26)

## [0.1.0] - 2026-05-04

### Added
- Initial public release: install.sh, scripts, systemd units, configs
- USB gadget mode and 15 baseline tweaks
- Auto-update system, captive portal auto-login, reproducible install
- AGENTS.md context document for AI-assisted contributors
- GL-MT3000 synergy guide
- CI workflow, `clone-mac.sh`, documentation refresh

### Fixed
- All shellcheck warnings cleared in baseline scripts

[Unreleased]: https://github.com/NicoMancinelli/pi-travel-router/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.9.1...v1.0.0
[0.9.1]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.8.3...v0.9.0
[0.8.3]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/NicoMancinelli/pi-travel-router/releases/tag/v0.1.0
