# firstboot — pre-built-image setup wizard

A one-shot web wizard that runs on the very first boot of the pre-built SD card image, so non-technical users can configure the travel router without ever opening a terminal.

## What it does

1. On first boot, `firstboot.service` (a systemd unit) starts `server.py`, a stdlib-only Python HTTP server listening on port 80.
2. The user connects to the Pi over Ethernet/USB-gadget/temporary SSID and visits `http://travelrouter.local` (mDNS).
3. They fill in a single mobile-friendly form: AP SSID + passphrase, country code, optional Tailscale/SSH/ntfy settings, feature toggles.
4. Submitting the form writes `/var/lib/travel-router/firstboot-env.sh` (an `export ...` shell file with all values shell-escaped via `shlex.quote`).
5. The server spawns `install.sh` in the background with `INSTALL_NONINTERACTIVE=1`. Output streams to `/var/log/firstboot-install.log`.
6. The browser is redirected to `/status`, which auto-refreshes every 5 seconds and shows the last 30 log lines and a step-progress list parsed from install.sh section headers.
7. If `install.sh` exits non-zero, `/status` switches to an error view showing the last 50 log lines and a Retry button that resets state and returns to the form.
8. When `install.sh` completes successfully it `touch`es `/var/lib/travel-router/firstboot-done`, disables the unit, and reboots.
8. On the next boot, `ConditionPathExists=!/var/lib/travel-router/firstboot-done` keeps the wizard from coming back.

## Files

| File              | Purpose                                                        |
|-------------------|----------------------------------------------------------------|
| `server.py`       | Stdlib HTTP server. Validates form, writes env file, spawns install. |
| `index.html`      | Single-page form. Mobile-first, dark, no framework.            |
| `firstboot.service` | systemd unit. Runs as root so it can bind :80 and write under `/var/lib`. |

## How it activates

The image-build pipeline (`build/`, owned by another agent) copies `firstboot/` into `/opt/pi-travel-router/firstboot/` and enables the unit:

```
systemctl enable firstboot.service
```

Once `/var/lib/travel-router/firstboot-done` exists the unit's `ConditionPathExists=!` keeps it dormant on every subsequent boot.

## Contract with `install.sh`

When `INSTALL_NONINTERACTIVE=1` is set, `install.sh` skips every `read` prompt and uses these environment variables (the wizard sets all of them):

Required:
- `AP_PASS` (8-63 chars; install aborts if missing)

Defaulted automatically:
- `AP_SSID` (default `TravelRouter`)
- `COUNTRY` (default `US`)

Optional strings (default empty):
- `NTFY_TOPIC`, `TS_KEY`, `SSH_ADMIN_KEY`, `HEADSCALE_URL`, `SPLIT_TUNNEL_DOMAINS`, `TOR_AP_PASS`

Boolean flags (`0`/`1`, default `0` in non-interactive mode if unset):
- `ENABLE_VPN_KILLSWITCH`, `ENABLE_BLOCKLISTS`, `ENABLE_DOT`, `ENABLE_AUTO_UPDATES`,
  `ENABLE_AVAHI_REFLECTOR`, `ENABLE_ADGUARD`, `ENABLE_AP_SCHEDULE`,
  `ENABLE_CLIENT_QOS`, `ENABLE_CAKE_AUTOTUNE`, `ENABLE_BANDWIDTH_DASHBOARD`,
  `ENABLE_PROMETHEUS_EXPORTER`, `ENABLE_UPS_MONITOR`, `ENABLE_2FA`,
  `ENABLE_SPLIT_TUNNEL`, `ENABLE_WAN_METRICS`,
  `ENABLE_TOR_TRANSPARENT`, `ENABLE_HTTP_UA_REWRITE`,
  `ENABLE_OPEN_WIFI_FALLBACK`, `ENABLE_PER_DEVICE_VPN`

If `ENABLE_TOR_TRANSPARENT=1`, `TOR_AP_PASS` (8+ chars) must also be set.

## Raspberry Pi Imager pre-seed

When a `firstrun.sh` file written by Raspberry Pi Imager is present on the boot partition (`/boot/firmware/firstrun.sh` or `/boot/firstrun.sh`), the wizard reads it at startup and pre-fills the following fields automatically:

- **Hostname → AP SSID** — the hostname set in Imager becomes the default AP SSID.
- **SSH public key** — if Imager added an authorized key, it is pre-filled in the SSH admin key field.
- **AP passphrase** — if `AP_PASS` is present in `firstrun.sh`, it is pre-filled and the user can submit the wizard without typing a passphrase at all.

To pre-seed the AP passphrase via Imager, add the following line anywhere in `firstrun.sh` (or in the Imager "Advanced options" custom script field):

```
AP_PASS="your-passphrase-here"
```

The value must be at least 8 characters; shorter values are silently ignored and the field remains blank.

If `AP_PASS` is not present in `firstrun.sh`, the wizard will still require it to be entered interactively in the browser — the form will not submit without it.

**Security note:** `AP_PASS` stored in `firstrun.sh` is plaintext on the FAT boot partition, readable by anyone with physical access to the SD card. This is acceptable for personal/home use. Do not use this pre-seed mechanism on SD cards that will be shared or used in public environments; enter the passphrase interactively instead.

## Security notes

- Form fields are shell-escaped with `shlex.quote` before being written to the env file. Inputs that look like shell metacharacters can't break out.
- The env file is written with mode `0600` so only root can read the AP passphrase / Tailscale key.
- The wizard only listens during first boot; after the install completes the unit is disabled and the file `firstboot-done` blocks reactivation.
