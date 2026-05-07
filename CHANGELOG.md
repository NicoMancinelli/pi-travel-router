# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
