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
9. On the next boot, `ConditionPathExists=!/var/lib/travel-router/firstboot-done` keeps the wizard from coming back.

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
  `ENABLE_OPEN_WIFI_FALLBACK`, `ENABLE_PER_DEVICE_VPN`, `ENABLE_WIREGUARD`,
  `ENABLE_USB_SHARE`

If `ENABLE_TOR_TRANSPARENT=1`, `TOR_AP_PASS` (8+ chars) must also be set.

## Raspberry Pi Imager Provisioning & Drop-in Configuration

When writing an image with **Raspberry Pi Imager** (or mounting the FAT32 boot partition after writing), you can provision router settings in two ways:

### 1. Raspberry Pi Imager "OS Customisation" Options

When using Raspberry Pi Imager, the customization dialog can be accessed directly:
- **In Imager v2.0+**: If writing a custom local image, press **`Ctrl+Shift+X`** (or **`Cmd+Shift+X`** on macOS) to open the Advanced Options / Customisation dialog, OR launch Imager with our repository manifest: `rpi-imager --repo https://raw.githubusercontent.com/NicoMancinelli/pi-travel-router/main/os_list.json`.

The following options are automatically extracted and configured:

- **Hostname (`ROUTER_HOSTNAME`)** — sets the system hostname and default mDNS name (`hostname.local`).
- **User / Password** — password (or password hash) configured in Imager is automatically applied to `root`, replacing temporary console credentials. Temporary plaintext password files on the boot partition are shredded on first boot.
- **Wireless LAN Country (`COUNTRY`)** — sets the Wi-Fi regulatory domain (e.g. `US`, `GB`, `DE`).
- **Timezone (`ROUTER_TIMEZONE`)** — sets system timezone for cron jobs, AP schedules, and daily reports (e.g. `America/New_York`, `Europe/London`).
- **Wireless LAN SSID & Password (`AP_SSID`, `AP_PASS`)** — pre-seeds the Wi-Fi AP name and passphrase (min 8 characters).
- **SSH Public Keys (`SSH_ADMIN_KEY`)** — automatically written to `/root/.ssh/authorized_keys` with `0600` permissions and loaded into the wizard.

### 2. Boot-Partition Drop-in File (`travel-router.env`)

You can create a file named `travel-router.env` (or `travel-router.conf`) in the root of the boot partition (`/boot/firmware/` or `/boot/`). Any router variable or feature toggle can be defined:

```bash
# /boot/firmware/travel-router.env
AP_SSID="MyTravelRouter"
AP_PASS="SuperSecretPassphrase123"
COUNTRY="US"
ROUTER_HOSTNAME="travelrouter"
ROUTER_TIMEZONE="America/New_York"
SSH_ADMIN_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... user@host"
TS_KEY="tskey-auth-..."
NTFY_TOPIC="my-router-alerts"
ENABLE_BLOCKLISTS=1
ENABLE_ADGUARD=1
ENABLE_VPN_KILLSWITCH=1
ENABLE_AUTO_UPDATES=1
ENABLE_WIREGUARD=0

# Set AUTO_INSTALL=1 for headless zero-touch setup (requires AP_PASS >= 8 chars)
AUTO_INSTALL=1
```

### 3. Headless Zero-Touch Provisioning (`AUTO_INSTALL=1`)

If `AUTO_INSTALL=1` (or `HEADLESS=1`) is specified in `travel-router.env` or `firstrun.sh` along with a valid `AP_PASS` (>= 8 chars), the router will automatically start the non-interactive installation on first boot without requiring any browser interaction.
While the installation runs, the web server on port 80 streams real-time status on `http://travelrouter.local/status` (or `http://192.168.7.1/status`).

## Security notes

- Credentials (passwords, auth keys, drop-in configs) written to the FAT32 boot partition are migrated securely to `/var/lib/travel-router/` (mode `0600`) and the originals on the unencrypted boot partition are safely shredded on boot.
- Form fields are shell-escaped with `shlex.quote` before being written to the env file. Inputs that look like shell metacharacters cannot break out.
- The env file is written with mode `0600` so only root can read the AP passphrase / Tailscale key.
- The wizard only listens during first boot; after the install completes the unit is disabled and the file `firstboot-done` blocks reactivation.
- **The `/status` page is unauthenticated and streams install log output to anyone on the LAN while the wizard is active.** Never add `set -x` or equivalent to `install.sh` as it would expose secrets (Tailscale keys, AP passphrases) in the log.
