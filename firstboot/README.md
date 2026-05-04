# firstboot — pre-built-image setup wizard

A one-shot web wizard that runs on the very first boot of the pre-built SD card image, so non-technical users can configure the travel router without ever opening a terminal.

## What it does

1. On first boot, `firstboot.service` (a systemd unit) starts `server.py`, a stdlib-only Python HTTP server listening on port 80.
2. The user connects to the Pi over Ethernet/USB-gadget/temporary SSID and visits `http://travelrouter.local` (mDNS).
3. They fill in a single mobile-friendly form: AP SSID + passphrase, country code, optional Tailscale/SSH/ntfy settings, feature toggles.
4. Submitting the form writes `/var/lib/travel-router/firstboot-env.sh` (an `export ...` shell file with all values shell-escaped via `shlex.quote`).
5. The server spawns `install.sh` in the background with `INSTALL_NONINTERACTIVE=1`. Output streams to `/var/log/firstboot-install.log`.
6. The browser is redirected to `/status`, which auto-refreshes every 5 seconds and shows the last 20 log lines.
7. When `install.sh` completes successfully it `touch`es `/var/lib/travel-router/firstboot-done`, disables the unit, and reboots.
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

## Security notes

- Form fields are shell-escaped with `shlex.quote` before being written to the env file. Inputs that look like shell metacharacters can't break out.
- The env file is written with mode `0600` so only root can read the AP passphrase / Tailscale key.
- The wizard only listens during first boot; after the install completes the unit is disabled and the file `firstboot-done` blocks reactivation.
