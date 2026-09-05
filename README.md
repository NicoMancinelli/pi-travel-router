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

Everything is controlled via flags: DNS-over-TLS, VPN kill switch, split tunneling, per-device VPN routing, USB storage share, ntfy push notifications, Headscale support. Defaults are off; the install is idempotent; one config file (`/etc/default/travel-router`) controls everything.

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

The fastest path is the pre-built SD card image.

### Option A — Pre-built image (recommended, fastest)

Insert your SD card into your Mac or Linux machine, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/NicoMancinelli/pi-travel-router/main/scripts/flash.sh | bash
```

The script downloads the latest release, verifies the SHA256 checksum, prompts you to confirm the target device, and flashes. Takes 2–5 minutes. Done — skip to step 3 below.

> **Requires:** `curl`, `xz`, `dd` (all pre-installed on macOS and most Linux distros). You'll be asked for your password once for `sudo dd`.

---

### Option B — Install on a fresh Pi OS (no laptop needed)

If you already have a Raspberry Pi Zero 2 W running **Pi OS Lite Bookworm** (64-bit), SSH in and run:

```bash
curl -fsSL https://raw.githubusercontent.com/NicoMancinelli/pi-travel-router/main/scripts/setup.sh | sudo bash
```

The script:
1. Verifies you're on a Raspberry Pi running Bookworm
2. Installs `git` if it isn't present
3. Clones the repo to `/opt/pi-travel-router`
4. Launches the interactive installer (prompts for SSID, password, country, and optional features)

**Non-interactive / scripted mode:**

```bash
curl -fsSL .../scripts/setup.sh | sudo AP_PASS="mysecretpass" INSTALL_NONINTERACTIVE=1 bash
```

All `install.sh` env vars are passed through (see `install.sh` header for the full list).

The Pi reboots at the end. After reboot, connect via USB gadget (step 4 below).

---

### Option C — Manual flash (Raspberry Pi Imager)

**1. Download the image**

Grab the latest `travelrouter-*-arm64-lite.img.xz` from the [Releases](https://github.com/NicoMancinelli/pi-travel-router/releases) page.

**2. Flash the image**

Open [Raspberry Pi Imager](https://www.raspberrypi.com/software/):

- **Choose OS** → **Use custom** → select the `.img.xz` you downloaded
- **Choose Storage** → select your SD card
- **OS Customisation**:
  - *Option A (Imager Customization)*: In Imager v2.0+, press **`Ctrl+Shift+X`** (or **`Cmd+Shift+X`** on macOS) to open the Advanced Options / Customization dialog. Alternatively, launch Imager with our repository manifest: `rpi-imager --repo https://raw.githubusercontent.com/NicoMancinelli/pi-travel-router/main/os_list.json`. You can configure Hostname, Wi-Fi credentials & country, Timezone, User password, and SSH keys directly!
  - *Option B (Boot Drop-in)*: After flashing, simply drop a `travel-router.env` file (e.g. with `AUTO_INSTALL=1`, `AP_PASS="mysecretpassword"`) onto the FAT32 boot partition (`/Volumes/bootfs/` or `E:\`) for zero-touch setup without any Imager prompts.
- **Write**

---

**3. Boot the Pi**

Insert the SD card. Connect your laptop to the **USB** data port (the middle micro-USB port, next to HDMI). The laptop's USB-C port provides both power and data! Wait ~90 seconds for first boot (mDNS, network, and the firstboot wizard need to come up).

**4. Open the wizard**

The image pre-enables USB Ethernet gadget mode (CDC-ECM / `g_ether`), so your MacBook/laptop detects a new USB Ethernet adapter and receives a DHCP lease in `192.168.7.0/24`. Browse to:

```
http://192.168.7.1
```

- **macOS / Linux**: Native CDC-ECM support — appears automatically in **System Settings → Network** as `RNDIS/Ethernet Gadget` or `USB 10/100 LAN`.
- **Windows 10/11**: CDC-ECM/RNDIS driver assigns IP automatically.

If the Pi is already on a network you can reach (e.g. via pre-seeded Wi-Fi or USB Ethernet hub), `http://travelrouter.local` works too. SSH terminal: `ssh root@192.168.7.1` (SSH key required).

#### USB gadget not showing up on MacBook / Laptop?

| Symptom | Likely cause | Fix |
|---|---|---|
| Nothing in System Settings → Network | Wrong port | Connect to the **middle** micro-USB port (`USB`), NOT the outer one (`PWR IN` has no data pins) |
| Nothing in System Settings → Network | Charge-only cable | Swap for a 4-wire data cable — test with `system_profiler SPUSBDataType \| grep -i gadget` |
| Device appears but no IP / can't reach 192.168.7.1 | `config.txt` patch missing | Mount SD card, check `/Volumes/bootfs/config.txt` for `dtoverlay=dwc2,dr_mode=peripheral` and `cmdline.txt` for `modules-load=dwc2,g_ether` |
| Interface shows "Self-Assigned IP" on Mac | DHCP negotiation delay | Wait 15–30 seconds for NetworkManager to assign `192.168.7.x` via DHCP |

Quick check (macOS Terminal, Pi plugged into middle USB port):
```bash
system_profiler SPUSBDataType | grep -i -A8 "gadget\|ethernet\|rndis\|cdc\|linux"
# Should show: "RNDIS/Ethernet Gadget" or similar CDC-ECM device
```

**5. Fill in the form**

The wizard collects everything `install.sh` needs:

- AP SSID and passphrase (8+ chars)
- Wi-Fi country code
- Optional Tailscale auth key, SSH public key, ntfy topic, Headscale URL
- Feature toggles (DoT, kill switch, split tunnel, etc.)

Submit. The wizard writes a shell-escaped env file, kicks off `install.sh` in the background, and redirects you to a status page that tails `/var/log/firstboot-install.log`. The Pi reboots itself when the install finishes.

**6. Connect**

After reboot:

- Plug the Pi into your laptop with USB-C (the `USB` port, not `PWR`). The Pi appears as a USB Ethernet adapter; SSH at `192.168.7.1`.
- Connect your phones/tablets to the new AP SSID.
- If you provided a Tailscale key, the Pi is already on your tailnet advertising `10.3.141.0/24`.

**7. SSH access**

SSH key authentication is **required** — password login is disabled by design (`PasswordAuthentication no`, `PermitRootLogin prohibit-password`).

Add your SSH public key via one of these methods:

- **Raspberry Pi Imager** (recommended): click the ⚙ OS Customisation button before writing the card, go to the **Services** tab, and paste your public key there.
- **Firstboot wizard** (`http://travelrouter.local`): paste your public key in the "SSH Admin Key" field.

After setup:

```sh
ssh root@192.168.7.1          # or ssh root@travelrouter.local
```

> **Console-only password:** A temporary root password is written to `/boot/firmware/root-password.txt` on the boot partition for emergency console access only. It does **not** enable SSH password login. Change it with `passwd` after first boot, and delete the file.

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
ssh root@192.168.7.1        # SSH key required
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
Plug in your iPhone (USB tethering on) or Android phone — the Pi picks up the tether as a higher-priority uplink (iPhone USB: metric 100, Android USB: metric 200) and drops hotel WiFi automatically. No configuration needed.

---

## What the wizard sets up

A condensed view of what you get after the install completes. Full feature list lives in [`IMPROVEMENTS.md`](IMPROVEMENTS.md).

- **Wi-Fi AP** on `uap0` (hostapd, 802.11n HT40, DTIM=1 for iOS wake latency), `10.3.141.0/24`
- **USB Ethernet gadget** at `192.168.7.1/24` (laptop over USB-C)
- **Uplink failover** across iPhone USB (m=100), Android USB (m=200), Bluetooth PAN (m=300), Wi-Fi STA (m=600); 30s watchdog
- **WAN watchdog** with graduated recovery: reassociate -> restart dhcpcd -> reboot
- **Tailscale subnet router** advertising `10.3.141.0/24`; optional Headscale control server
- **Captive portal** detection, MAC clone helper (`clone-mac.sh`), and per-SSID auto-login hooks
- **Carrier bypass (Visible/Verizon):** TTL=65, IPv6 hop-limit=65, TCP MSS clamping to PMTU, DSCP strip, IPv6 ext-header drop (nftables `inet travel_mangle` + iptables defense-in-depth), DNS port 53 interception, dynamic IPv6 uplink suppression (USB/BT)
- **Stateful firewall:** `FORWARD DROP` with explicit ACCEPTs, AP client isolation, optional VPN kill switch
- **DNS:** dnsmasq with rebind protection; optional DNS-over-TLS via stubby
- **QoS:** TCP BBR congestion control, CAKE queue discipline
- **Reliability:** hardware watchdog, log2ram, log rotation, unattended security updates
- **Observability:** `travel-tui` dashboard, web dashboard (`:8080`), `travel-status`, ntfy push
- **Travel NAS:** optional USB drive sharing over SMB to AP clients (`ENABLE_USB_SHARE=1`, guest access, never exposed on uplinks)
- **SD card protection:** read-only root (overlayfs) toggle — `sudo overlayfs-ctl.sh enable` (disable before updates)
- **Split Tunnel:** CIDR-based split tunneling (`ENABLE_WG_SPLIT_TUNNEL=1`) and per-device VPN routing (`ENABLE_PER_DEVICE_VPN=1`)

---

## Management

Three management interfaces are available:

**SSH terminal**

```sh
sudo travel-tui          # interactive TUI dashboard: uplink, AP clients, feature toggles, logs
sudo travel-status       # one-shot status snapshot
sudo update-router.sh    # pull latest from GitHub and re-run changed install steps
```

**Web dashboard** — available at `http://192.168.7.1:8080` (or `http://travelrouter.local:8080`). Provides a dark-UI browser interface for uplink status, peer management, privacy profile switching, and system diagnostics. No additional install needed — the Flask server starts automatically.

**TUI** (`travel-tui`) — SSH in and run `sudo travel-tui` for a full-screen terminal dashboard.

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

`iptables-nft` for filter/NAT (FORWARD policy `DROP`, ESTABLISHED/RELATED fast path, explicit per-uplink ACCEPTs, NAT masquerade across all uplinks, client port 53 DNS interception, AP client isolation, optional `KILL_SWITCH` chain). Native nftables `inet travel_mangle` table handles TTL=65, IPv6 hop-limit=65, DSCP CS0 strip, TCP MSS clamping to PMTU, and IPv6 extension-header drop across IPv4 and IPv6.

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
ENABLE_OPEN_WIFI_FALLBACK="0"
ENABLE_DOT="0"
ENABLE_VPN_KILLSWITCH="0"
ENABLE_AUTO_UPDATES="0"
ENABLE_AVAHI_REFLECTOR="0"
ENABLE_PER_DEVICE_VPN="0"
VPN_DEVICE_MACS=""

ENABLE_WG_SPLIT_TUNNEL="0"
WG_SPLIT_TUNNEL_CIDRS=""
WG_SPLIT_TUNNEL_DEV="tailscale0"

ENABLE_WAN_METRICS="1"
ENABLE_USB_SHARE="0"           # Travel NAS: share /media/travel-data over SMB.
USB_SHARE_NAME="TravelData"

ENABLE_WIREGUARD="0"
```

For non-interactive installs, the same flags can be exported as env vars before `install.sh` runs (with `INSTALL_NONINTERACTIVE=1`). Full env-var contract: [`firstboot/README.md`](firstboot/README.md).

After flipping a flag, reload firewall or restart the relevant service:

```sh
sudo travel-router-firewall.sh --save     # ENABLE_VPN_KILLSWITCH
sudo systemctl restart stubby             # ENABLE_DOT
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

**Current version:** see [`VERSION`](VERSION) — all originally planned features shipped; see [`CHANGELOG.md`](CHANGELOG.md) for ongoing releases.

### ✅ Shipped (v2.0–v2.3)

| Feature | Version |
|---|---|
| WireGuard VPN + kill switch | v2.0 |
| Web management dashboard (Flask + dark UI) | v2.0 |
| Python/Textual TUI | v2.0 |
| Modular installer (`--dry-run`, `--module`, `--skip`) | v2.0 |
| OTA A/B slot updates with GPG verify | v2.0 |
| Captive portal auto-login (5 templates) | v2.0 |
| 4G/LTE modem support (ModemManager) | v2.0 |
| IPv6 first-class (SLAAC, DHCPv6, ip6tables) | v2.0 |
| Observability (structured logs, ntfy alerts, log rotation) | v2.0 |
| `curl \| bash` one-liner setup (`scripts/setup.sh`) | v2.2 |
| SD card flash script (`scripts/flash.sh`) | v2.2 |
| Quick/Standard/Expert installer TUI | v2.2 |
| Existing-install detection (upgrade/reconfigure/repair) | v2.2 |
| Debian Trixie / any Pi OS release compatibility | v2.2 |
| fail2ban (SSH + web dashboard brute-force protection) | v2.3 |
| WireGuard key rotation (monthly timer) | v2.3 |
| AIDE file integrity monitoring (daily, ntfy alert) | v2.3 |
| WireGuard peer management (expiry, QR, remove, TUI) | v2.3 |
| Privacy profiles (VPN-only / Adblock / Tor / Direct) | v2.3 |
| Integration test suite (31 tests: failover, OTA, API) | v2.3 |
| ARM64 native CI | v2.3 |

### 🗺 Up next (v2.4)

- **Captive portal improvements**: auto-detect network type, retry on failure
- **Mobile app companion**: iOS/Android shortcut to switch privacy profiles
- **Headscale integration**: self-hosted mesh VPN (WireGuard-based)
- **Per-device bandwidth quotas**: QoS rules per connected client MAC
- **Guest network**: isolated VLAN for untrusted devices

Full deployed feature list and outstanding roadmap items live in [`IMPROVEMENTS.md`](IMPROVEMENTS.md). Image build pipeline docs: [`build/README.md`](build/README.md). Firstboot wizard internals: [`firstboot/README.md`](firstboot/README.md).

---

## License

MIT. See `LICENSE`.
