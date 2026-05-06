# pi-travel-router

A self-contained travel router built on a Raspberry Pi Zero 2 W that shares any uplink (iPhone USB, Android USB, Bluetooth PAN, or hotel WiFi) as a private, Tailscale-connected Wi-Fi AP.

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Platform: Pi Zero 2W](https://img.shields.io/badge/platform-Pi%20Zero%202W-c51a4a.svg)
![OS: Pi OS Bookworm](https://img.shields.io/badge/OS-Pi%20OS%20Bookworm-green.svg)
[![Build Pi Image](https://github.com/NicoMancinelli/pi-travel-router/actions/workflows/build-image.yml/badge.svg)](https://github.com/NicoMancinelli/pi-travel-router/actions/workflows/build-image.yml)

Plug it into your laptop via USB-C and it appears as both a USB Ethernet adapter (192.168.7.1) and a Wi-Fi AP, routing connected devices through whatever uplink is available. Re-flashable, reproducible, scripted from a single repo.

---

## What is it?

Travel networks are hostile. Hotel Wi-Fi runs captive portals that re-auth every 8 hours and inject DNS responses. Carriers fingerprint tethered traffic via TTL, DSCP, and TCP options to throttle or block "hotspot" use of unlimited plans. Public APs sit between you and your traffic with no encryption guarantees, and you usually can't reach your home services from any of them.

This project turns one Pi Zero 2 W into the box that solves all of that at once. It is a single AP your devices stay associated to no matter where you are. Behind it, an uplink failover watchdog promotes whichever of your iPhone, Android, Bluetooth tether, or hotel Wi-Fi is currently working, and a TTL/hop-limit/DSCP mangler hides the fact that you're tethered. Tailscale runs as a subnet router so your laptop on the AP can reach home as if it were on your home LAN, and a captive-portal detector pauses Tailscale automatically while you sign in to hotel Wi-Fi, then restores it.

Everything is opt-in via flags: DNS-over-TLS, AdGuard Home, VPN kill switch, Tor transparent SSID, threat-intel IP blocklists, per-client QoS, scheduled AP disable, ntfy push notifications, automatic security updates, Headscale support. Defaults are off; the install is idempotent; one config file (`/etc/default/travel-router`) controls everything.

---

## Hardware

| Item | Notes |
|---|---|
| Raspberry Pi Zero 2 W | Required. The installer targets this board only. |
| MicroSD card, 8 GB+ | Class 10 or better. 16 GB recommended for log storage. |
| USB-C to Micro-USB cable | Connects Pi to laptop or iPhone USB-C hub. Carries both power and USB gadget data. |
| USB hub (optional) | Allows iPhone tethering and laptop connection simultaneously via the same Micro-USB port. |

---

## Quick Start

The fastest path is the pre-built SD card image. No terminal required to get a working router.

**1. Download the image**

Grab the latest `travelrouter-*-arm64-lite.img.xz` from the [Releases](https://github.com/NicoMancinelli/pi-travel-router/releases) page.

**2. Flash the image**

Open [Raspberry Pi Imager](https://www.raspberrypi.com/software/):

- **Choose OS** -> **Use custom** -> select the `.img.xz` you downloaded
- **Choose Storage** -> select your SD card
- **Write**

Imager handles `xz` decompression and writes the SD card. (CLI alternative:
```bash
xz -d travelrouter-*.img.xz
diskutil unmountDisk /dev/diskN  # macOS: required before dd
sudo dd if=travelrouter-*.img of=/dev/diskN bs=4M status=progress conv=fsync
```
Replace `/dev/diskN` with your SD card device — use `diskutil list` on macOS or `lsblk` on Linux to identify it.)

**3. Boot the Pi**

Insert the SD card. Connect power to the `PWR` port. (The `PWR` port is the one closer to the **edge** of the board, labeled `PWR IN`. The `USB` port in the next step is the one in the middle.) Wait ~90 seconds for first boot (mDNS, network, and the firstboot wizard need to come up). Wait for the USB Ethernet adapter to appear on your laptop before opening a browser.

**4. Open the wizard**

Plug the Pi's `USB` port (not `PWR`) into your laptop with a USB-C cable. The image pre-enables USB gadget mode (CDC NCM / `g_ncm`), so the laptop sees a new USB Ethernet device and gets a DHCP lease in `192.168.7.0/24`. Browse to:

```
http://192.168.7.1
```

- **Linux / macOS**: the USB device appears automatically; DHCP lease arrives within a few seconds.
- **Windows 10/11**: uses CDC NCM — inbox driver, no installation needed. The device may take 10–15 seconds to enumerate on first use.

If the Pi is already on a network you can reach (e.g. via a USB Ethernet hub or pre-seeded Wi-Fi), `http://travelrouter.local` works too. SSH terminal: `ssh root@192.168.7.1` (password `changeme`).

**5. Fill in the form**

The wizard collects everything `install.sh` needs:

- AP SSID and passphrase (8+ chars)
- Wi-Fi country code
- Optional Tailscale auth key, SSH public key, ntfy topic, Headscale URL
- Feature toggles (DoT, AdGuard, kill switch, blocklists, etc.)

Submit. The wizard writes a shell-escaped env file, kicks off `install.sh` in the background, and redirects you to a status page that tails `/var/log/firstboot-install.log`. The Pi reboots itself when the install finishes.

**6. Connect**

After reboot:

- Plug the Pi into your laptop with USB-C (the `USB` port, not `PWR`). The Pi appears as a USB Ethernet adapter; SSH at `192.168.7.1`.
- Connect your phones/tablets to the new AP SSID.
- If you provided a Tailscale key, the Pi is already on your tailnet advertising `10.3.141.0/24`.

**7. Change the root password**

The image ships with `root` / `changeme`. After first boot:

```sh
ssh root@travelrouter.local
passwd
```

If you supplied an SSH public key in the wizard, password authentication is automatically disabled by `install.sh` and only key auth remains.

---

## Hotel check-in workflow

On arrival at a hotel or conference WiFi:

**1. Power on the Pi.**
Plug the `PWR` port into any USB power source. Wait ~30 seconds for boot.

**2. Plug into your laptop via USB-C.**
The laptop gets a DHCP address in `192.168.7.0/24`. Your devices can now use the Pi's AP — they just have no internet yet.

**3. Connect the Pi to the hotel WiFi.**
SSH in and run:

```sh
ssh root@192.168.7.1        # password: changeme (or your set password)
nmcli dev wifi list          # scan — find the hotel SSID
nmcli dev wifi connect "Hotel WiFi Name" password "roompassword"
# For open networks (no password):
nmcli dev wifi connect "Hotel WiFi Name"
```

Or use the TUI: `sudo travel-tui` → **[5] Network Tools** → **[7] Connect to hotel/new WiFi**.

> **Note:** The Pi Zero 2 W has a single 2.4 GHz radio. Switching hotel networks shifts the AP channel, briefly disconnecting AP clients — they reconnect automatically within a few seconds.

**4. Handle the captive portal.**
If the hotel uses a captive portal (login page), the Pi's wan-watchdog detects it automatically:

- Tailscale pauses (so the portal redirect isn't blocked by the VPN).
- You get an ntfy push notification (if configured) with a high-priority alert.
- Open a browser on any device connected to the Pi's AP and browse to any `http://` site — you'll be redirected to the hotel portal.
- Complete login. The Pi re-probes every 60 seconds and restores Tailscale once internet is clear.

For stubborn portals or re-auth loops:

```sh
# If the portal only authenticates the Pi's MAC but your laptop was auth'd before:
sudo travel-tui   # → [5] Network → [3] Clone MAC to wlan0
# Run captive check manually:
sudo /usr/local/bin/captive-check.sh
```

**5. Reconnect periodically.**
Many hotel portals expire every 4–12 hours. The watchdog detects re-auth and notifies you via ntfy. Tailscale auto-restores after each re-login.

**6. Switch uplinks on the fly.**
Plug in your iPhone (USB tethering on) or Android phone — the Pi picks up the tether as a higher-priority uplink (metric 100) and drops hotel WiFi automatically. No configuration needed.

---

## What the wizard sets up

A condensed view of what you get after the install completes. Full feature list lives in [`IMPROVEMENTS.md`](IMPROVEMENTS.md).

- **Wi-Fi AP** on `uap0` (hostapd, 802.11n HT40, DTIM=1 for iOS wake latency), `10.3.141.0/24`
- **USB Ethernet gadget** at `192.168.7.1/24` (laptop over USB-C)
- **Uplink failover** across iPhone USB (m=100), Android USB (m=200), Bluetooth PAN (m=300), Wi-Fi STA (m=600); 30s watchdog
- **WAN watchdog** with graduated recovery: reassociate -> restart dhcpcd -> reboot
- **Tailscale subnet router** advertising `10.3.141.0/24`; optional Headscale control server
- **Captive portal** detection, MAC clone helper (`clone-mac.sh`), and per-SSID auto-login hooks
- **Carrier bypass:** TTL=65, IPv6 hop-limit=65, DSCP strip, IPv6 ext-header drop (nftables `inet travel_mangle`)
- **Stateful firewall:** `FORWARD DROP` with explicit ACCEPTs, AP client isolation, optional VPN kill switch
- **DNS:** dnsmasq with rebind protection; optional DNS-over-TLS via stubby; optional AdGuard Home
- **Privacy opt-ins:** Tor transparent SSID, Firehol L1 blocklist, MAC randomization, HTTP UA rewrite
- **QoS:** CAKE bufferbloat (with optional weekly auto-tune), TCP BBR, per-client fairness
- **Reliability:** hardware watchdog, log2ram, log rotation, unattended security updates
- **Observability:** `travel-tui` dashboard, `travel-status`, ntfy push, daily digest, Tailscale peer watchdog, optional Prometheus exporter, optional bandwidth HTML dashboard
- **Hardware:** optional PiSugar 3 UPS monitor with safe shutdown
- **2FA:** optional SSH TOTP via `setup-2fa.sh`

---

## Management

```sh
sudo travel-tui          # interactive dashboard: uplink, AP clients, feature toggles, logs
sudo travel-status       # one-shot status snapshot
sudo update-router.sh    # pull latest from GitHub and re-run changed install steps
```

Single config file: `/etc/default/travel-router`. The TUI's Features screen toggles any `ENABLE_*` flag live, reloading the firewall when the change requires it.

The TUI gives you:
- **Dashboard** — live uplink status, Tailscale state, bandwidth counters, AP clients (with IPs), system health, Wi-Fi RSSI when on hotel WiFi uplink
- **Features** — toggle any `ENABLE_*` flag with an immediate service restart
- **Settings** — edit all config vars (AP credentials, Bluetooth MAC, ntfy topic, Tailscale args, etc.)
- **Network** — start/stop uplinks, Bluetooth tether, view routing table
- **Services** — status of all router services at a glance
- **Logs** — tail key logs (WAN watchdog, Tailscale, hostapd, failover)
- **System** — reboot, update, run diagnostics, configure 2FA

All changes write directly to `/etc/default/travel-router` and take effect immediately. No need to edit config files manually.

**RaspAP** web UI is available at `http://10.3.141.1` (credentials: `admin` / `secret`). It provides a graphical interface for hostapd/dnsmasq configuration. The `travel-tui` covers most management tasks, but RaspAP can be useful for advanced WiFi tuning.

---

## Updates

Updates are git-pull based, not image-based:

```sh
sudo update-router.sh
```

The script checks GitHub Releases, falls back to the latest `main` SHA, and re-runs any changed install steps. The installer is idempotent — running it on an already-configured Pi is safe.

A weekly systemd timer (Sun 03:00) does the same thing automatically when `ENABLE_AUTO_UPDATES=1`. New `.img.xz` releases are only cut for major OS bumps (e.g. Bookworm -> Trixie) or to refresh the bootstrap baseline.

---

## Advanced features

### Per-SSID portal scripts

Place a shell script at `/etc/travel-router/portals/<SSID>.sh` to run custom captive-portal login logic automatically when the Pi connects to that SSID. The script is invoked by `captive-check.sh` after portal detection. See `scripts/portals/` for example scripts (accept-terms, credential submit) and `scripts/portals/README.md` for the hook contract.

### Wi-Fi RSSI

When the active uplink is hotel/open WiFi (`wlan0`), the signal level (RSSI in dBm) is shown inline in the `travel-tui` dashboard and in the output of `travel-status`.

### AP client IPs

The TUI dashboard shows connected AP clients alongside their IP addresses (not just a count), making it easy to identify devices and check DHCP leases at a glance.

---

## Advanced / Manual install

For developers, or to install on an existing Pi OS Lite Bookworm system without re-flashing.

**1. Flash Pi OS Lite Bookworm (64-bit)**

Use Raspberry Pi Imager, select "Raspberry Pi OS Lite (64-bit)". Before writing, click the **OS Customisation** (⚙) button to set your username, password, and SSH public key — this is required because Bookworm has no default `pi` user. Enable SSH in the Services tab. Write the card.

**2. SSH in**

```sh
ssh <your-username>@<hostname>.local   # use the username and hostname you set in Imager
```

**3. Clone and install**

```sh
git clone https://github.com/NicoMancinelli/pi-travel-router.git && cd pi-travel-router
sudo bash install.sh
```

The installer prompts for AP SSID/passphrase, country code, ntfy topic, Tailscale key, SSH admin pubkey, Headscale URL, and each `ENABLE_*` feature flag. All optional features default off.

For scripted installs, set `INSTALL_NONINTERACTIVE=1` plus the env vars listed in [`firstboot/README.md`](firstboot/README.md) — the same contract the web wizard uses.

**4. Reboot**

```sh
sudo reboot
```

After reboot, plug the Pi's `USB` port into your laptop — the USB gadget (CDC NCM / g_ncm) will be active. SSH at `192.168.7.1`.

---

## Architecture overview

**Interfaces**

| Interface | Role | Subnet | Metric |
|---|---|---|---|
| `uap0` | Wi-Fi AP (hostapd) | 10.3.141.0/24 | — |
| `usb0` | USB Ethernet gadget (CDC NCM / g_ncm) | 192.168.7.0/24 | — |
| `enx*` | iPhone USB tether | DHCP | 100 |
| `rndis0` / `usb0` | Android USB tether | DHCP | 200 |
| `bnep0` | Bluetooth PAN tether | DHCP | 300 |
| `wlan0` | Wi-Fi STA (hotel/home) | DHCP | 600 |
| `tailscale0` | Tailscale mesh | 100.x.x.x | — |

A NetworkManager dispatcher (`50-wan-metrics`) re-applies the metric ordering on every `ifup` so the failover watchdog can promote the lowest-metric working uplink without manual intervention.

**Failover & watchdogs**

- Uplink watchdog: 30s timer; promotes lowest-metric uplink that has connectivity.
- WAN watchdog: 60s timer; graduated recovery (reassociate -> restart dhcpcd -> reboot) when the active uplink stalls.
- Captive-portal probe: hits `generate_204`; pauses Tailscale during portal auth and restores it after.
- Tailscale peer watchdog: 5-min health check; ntfy alert on daemon down, stale handshake, or peer loss.

**Tailscale**

Runs as a subnet router advertising `10.3.141.0/24`. AP clients reach your tailnet via the Pi without a Tailscale install. Optional Headscale support points `tailscale up --login-server` at your own VPS. To automate Headscale installation on a public VPS, run `tools/setup-headscale.sh` on that server — the `HEADSCALE_URL` it outputs is then passed to the installer (via the wizard or `install.sh`).

**Firewall**

`iptables-nft` for filter/NAT (FORWARD policy `DROP`, ESTABLISHED/RELATED fast path, explicit per-uplink ACCEPTs, AP client isolation, optional `KILL_SWITCH` chain). Native nftables `inet travel_mangle` table handles TTL=65, IPv6 hop-limit=65, DSCP strip, and IPv6 extension-header drop in one ruleset across IPv4 and IPv6.

---

## Configuration

All runtime config lives in `/etc/default/travel-router`, sourced by every router script. Representative keys:

```sh
# Notifications
NTFY_TOPIC=""

# Bluetooth tethering
IPHONE_BT_MAC=""

# Tailscale
TAILSCALE_UP_ARGS="--advertise-routes=10.3.141.0/24 --accept-dns=false"
HEADSCALE_URL=""

# WAN watchdog probe targets
WAN_PING_TARGETS="1.1.1.1 8.8.8.8"

# Feature flags (0 = off, 1 = on)
ENABLE_DOT="0"
ENABLE_VPN_KILLSWITCH="0"
ENABLE_ADGUARD="0"
ENABLE_BLOCKLISTS="0"
ENABLE_TOR_TRANSPARENT="0"
ENABLE_HTTP_UA_REWRITE="0"
ENABLE_OPEN_WIFI_FALLBACK="0"
ENABLE_AVAHI_REFLECTOR="0"
ENABLE_AP_SCHEDULE="0"
ENABLE_CLIENT_QOS="0"
ENABLE_PER_DEVICE_VPN="0"
ENABLE_AUTO_UPDATES="0"
ENABLE_CAKE_AUTOTUNE="0"
ENABLE_SPLIT_TUNNEL="0"
ENABLE_2FA="0"
ENABLE_BANDWIDTH_DASHBOARD="0"
ENABLE_PROMETHEUS_EXPORTER="0"
ENABLE_UPS_MONITOR="0"
ENABLE_WAN_METRICS="1"         # Per-interface RX/TX accounting via vnstat. Default: 1 (on).

AP_DISABLE_TIME="02:00"
AP_ENABLE_TIME="07:00"
AP_CLIENT_BANDWIDTH="unlimited"
VPN_DEVICE_MACS=""
SPLIT_TUNNEL_DOMAINS=""
MAX_BLOCKLIST_ENTRIES="20000"
```

For non-interactive installs, the same flags can be exported as env vars before `install.sh` runs (with `INSTALL_NONINTERACTIVE=1`). Full env-var contract: [`firstboot/README.md`](firstboot/README.md).

After flipping a flag, reload firewall or restart the relevant service:

```sh
sudo travel-router-firewall.sh --save     # ENABLE_VPN_KILLSWITCH, ENABLE_BLOCKLISTS, ENABLE_TOR_TRANSPARENT
sudo systemctl restart stubby             # ENABLE_DOT
sudo systemctl restart adguard-home       # ENABLE_ADGUARD
sudo systemctl restart avahi-daemon       # ENABLE_AVAHI_REFLECTOR
```

The `travel-tui` Features screen handles all of this for you when toggling.

---

## Troubleshooting

**`travelrouter.local` doesn't resolve**

mDNS doesn't traverse some networks. Find the Pi's IP in your router's DHCP table and use it directly. On Linux, `avahi-resolve -n travelrouter.local` confirms whether mDNS is reaching the Pi at all.

**Wizard hangs / status page never finishes**

```sh
ssh root@travelrouter.local
tail -f /var/log/firstboot-install.log
```

The install runs `install.sh` in the background; the log shows the actual progress. Common causes: bad Tailscale auth key, no internet on the bootstrap network, package mirror failure.

**SSH suddenly demands a TOTP code**

You enabled `ENABLE_2FA=1` but never ran the per-user enrollment. Log in via console (or USB gadget at `192.168.7.1`) and run:

```sh
setup-2fa.sh
```

That generates the QR code for your authenticator app.

**Tailscale auth failed during install**

Auth keys expire. Get a fresh one from the Tailscale admin console and run manually:

```sh
sudo tailscale up --advertise-routes=10.3.141.0/24 --accept-dns=false --authkey=tskey-auth-...
```

Add `--login-server=$HEADSCALE_URL` if you're using Headscale.

**iPhone USB tether not detected**

iPheth needs a one-shot pairing trust from the iPhone. Plug the iPhone into the Pi while unlocked, accept the "Trust this computer?" prompt, and re-plug. The udev rule (`config/90-ipheth.rules`) fires `start-tether.sh` on `enx*` add. `journalctl -u NetworkManager -f` while plugging in shows whether the interface comes up.

**WAN watchdog reboot loop**

```sh
journalctl -u wan-watchdog -n 200
```

Usually means none of `WAN_PING_TARGETS` are reachable from any uplink — the watchdog escalates to reboot when reassociate + dhcpcd restart both fail. Edit `/etc/default/travel-router` to use a target you know responds (some networks block `1.1.1.1`).

**Captive portal grabs you in a loop**

Run `clone-mac.sh <your-laptop-mac>` to make wlan0 present the same MAC the portal already authenticated. `clone-mac.sh --restore` resets to the randomized MAC.

**Can't reach AP clients from the Pi (or vice versa)**

That's by design — AP client isolation is in the FORWARD chain and INPUT blocks AP clients from port 22/80. Manage the Pi via the USB gadget (`192.168.7.1`) or Tailscale.

---

## Recovery scenarios

**AP won't start after reboot:**

```sh
sudo journalctl -u hostapd -n 50
sudo hostapd -d /etc/hostapd/hostapd.conf   # test config interactively (Ctrl-C to stop)
```

Common causes: wrong country code, DFS channel (try `channel=6` in `/etc/hostapd/hostapd.conf`), or `wlan0` not associated before hostapd started.

**Lost Tailscale + key-only SSH (locked out):**
Connect via USB gadget: `ssh root@192.168.7.1`. The USB gadget is always active and bypasses Tailscale. From there, re-auth Tailscale: `sudo tailscale up`.

**WAN watchdog reboot loop:**

```sh
sudo systemctl stop wan-watchdog.timer   # stop the loop first!
sudo journalctl -u wan-watchdog -n 30
# Fix the underlying connectivity issue, then:
sudo systemctl start wan-watchdog.timer
```

**Router not accessible at all:**
The USB gadget interface (`192.168.7.1`) is always available regardless of WiFi/Tailscale state. If even that fails, reflash the SD card using Raspberry Pi Imager with the latest image from Releases.

---

## Project status / roadmap

Full deployed feature list and outstanding roadmap items live in [`IMPROVEMENTS.md`](IMPROVEMENTS.md). Image build pipeline docs: [`build/README.md`](build/README.md). Firstboot wizard internals: [`firstboot/README.md`](firstboot/README.md).

---

## License

MIT. See `LICENSE`.
