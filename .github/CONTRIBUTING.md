# Contributing to pi-travel-router

Thank you for taking the time to contribute. This is a hobbyist open-source project and all constructive help is welcome — bug reports, documentation fixes, and code improvements alike.

---

## Reporting a bug

Use the [Bug report](.github/ISSUE_TEMPLATE/bug_report.md) issue template. Before filing, check whether the same issue already exists in the [issue tracker](https://github.com/NicoMancinelli/pi-travel-router/issues).

Include:

- The output of `travel-diagnostic` (run on the Pi: produces a redacted tar.gz of logs and config)
- Your router version (`cat /etc/travel-router-image-version` for image installs, or `cat VERSION` in the repo)
- How you installed (image flash + wizard, or manual `install.sh`)

## Suggesting a feature

Use the [Feature request](.github/ISSUE_TEMPLATE/feature_request.md) issue template. Ground the request in a real use case, and note the hardware constraints: Pi Zero 2 W has a single-core 1 GHz ARM CPU, 512 MB RAM, and one USB port shared between power delivery and gadget mode.

---

## Dev setup

### Building the image locally

The image build uses [pi-gen](https://github.com/RPi-Distro/pi-gen) and **requires Linux** (binfmt + qemu-user-static). macOS is not supported for native builds.

```sh
git clone https://github.com/RPi-Distro/pi-gen
cd pi-gen
sudo apt install -y coreutils quilt parted qemu-user-static debootstrap zerofree zip \
    dosfstools libarchive-tools libcap2-bin grep rsync xz-utils file git curl bc \
    qemu-utils kpartx gpg pigz arch-test
cp /path/to/pi-travel-router/build/config ./config
ln -s /path/to/pi-travel-router/build/stage-travel-router ./stage-travel-router
touch stage2/SKIP_IMAGES
REPO_URL=https://github.com/NicoMancinelli/pi-travel-router.git GIT_REF=main \
    sudo -E ./build.sh
```

Output lands in `pi-gen/deploy/`. See [`build/README.md`](build/README.md) for full details.

If you don't have a Linux host, trigger a build in GitHub Actions instead: **Actions → Build Pi Image → Run workflow** (produces a 14-day artifact).

### Testing shell script changes without a full image build

Most changes are in `scripts/` or `install.sh`. You can validate them without reflashing:

**Syntax check (any shell):**

```sh
bash -n scripts/my-script.sh
```

**Shellcheck (must pass before merge):**

```sh
shellcheck scripts/my-script.sh
# Or all scripts at once:
shellcheck scripts/*.sh install.sh
```

The CI `shellcheck.yml` workflow runs shellcheck on every push. Fix all warnings before opening a PR — the hook will block the merge otherwise.

**Live on-device test:**

The simplest workflow is to `git pull` on a running Pi and re-run the relevant part of `install.sh`. The installer is idempotent — running it on an already-configured Pi is safe. For targeted re-runs:

```sh
# Re-apply firewall only:
sudo bash scripts/travel-router-firewall.sh --save

# Re-run the full installer (safe, idempotent):
sudo bash install.sh
```

---

## Key files

| Path | Purpose |
|---|---|
| `install.sh` | Main installer; idempotent; reads env vars in non-interactive mode |
| `scripts/` | All runtime scripts deployed to `/usr/local/bin/` on the Pi |
| `scripts/travel-tui.sh` | Interactive TUI dashboard |
| `scripts/wan-watchdog.sh` | Uplink failover + graduated WAN recovery |
| `scripts/travel-router-firewall.sh` | nftables + iptables-nft ruleset |
| `scripts/captive-check.sh` | Captive portal probe; pauses/resumes Tailscale |
| `scripts/travel-diagnostic.sh` | Collects redacted logs + config into a tar.gz |
| `config/` | Static config files (hostapd, dnsmasq, stubby, AdGuard, nftables) |
| `systemd/` | Systemd unit files |
| `firstboot/` | First-boot web wizard (Python stdlib HTTP server + HTML form) |
| `build/` | pi-gen stage and config for the SD card image |
| `/etc/default/travel-router` | Single runtime config file (on the Pi) |

---

## PR conventions

- **One fix or feature per PR.** Keep the diff reviewable.
- **Commit message style** — match the existing history:
  - `fix: <short description>` — bug fix
  - `feat: <short description>` — new feature or opt-in capability
  - `docs: <short description>` — documentation only
  - `chore: <short description>` — build, CI, dependency, or tooling change
- **Shell scripts:** `bash -n` and `shellcheck` must pass. The CI workflow enforces this.
- **CHANGELOG:** update `CHANGELOG.md` under `[Unreleased]` for any user-visible change.
- **No new runtime dependencies** without discussion — the Pi Zero 2 W has 512 MB RAM and a slow SD card. Every additional package matters.

---

## Code style

- Shell scripts use `set -euo pipefail`. New scripts should too.
- Avoid bashisms that shellcheck flags. Target `bash` (not `sh`) since the shebang is `#!/bin/bash` throughout.
- Python (firstboot wizard) must pass `flake8` and `pylint` as enforced by `python-lint.yml`.

---

## License

By contributing you agree that your changes will be released under the [MIT License](../LICENSE) that covers this project.
