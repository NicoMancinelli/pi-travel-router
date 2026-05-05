# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- Per-client bandwidth fairness and per-device Tailscale routing (#21 #44)
- TUI dashboard, `status` command, MOTD, uplink alerts, AP scheduling, Wi-Fi QR helper

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
