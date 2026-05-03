# pi-travel-router

A self-contained travel router built on a Raspberry Pi Zero 2 W that shares any uplink (iPhone USB, Android USB, Bluetooth PAN, or hotel WiFi) as a private, Tailscale-connected Wi-Fi AP.

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Platform: Pi Zero 2W](https://img.shields.io/badge/platform-Pi%20Zero%202W-c51a4a.svg)
![OS: Pi OS Bookworm](https://img.shields.io/badge/OS-Pi%20OS%20Bookworm-green.svg)

It plugs into your laptop via USB-C and appears as both a USB Ethernet adapter (192.168.7.1) and a Wi-Fi AP, routing all connected devices through whatever uplink is available. Optional features include DNS-over-TLS, AdGuard Home, a VPN kill switch, Tor transparent proxy, threat-intel IP blocklists, and automatic failover across four uplink types. The goal is a reproducible, auditable setup you can re-flash and re-run from scratch in under 20 minutes.

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

These steps take you from a blank SD card to a working router.

**1. Flash Pi OS Lite Bookworm (64-bit)**

Use Raspberry Pi Imager: https://www.raspberrypi.com/software/

Select "Raspberry Pi OS Lite (64-bit)" — the desktop image is not needed.

**2. Enable SSH before first boot**

Mount the `bootfs` partition and create an empty file named `ssh` at its root:

```bash
touch /Volumes/bootfs/ssh
```

On Windows, create a file named `ssh` (no extension) in the boot partition root.

**3. Boot the Pi and SSH in**

Connect the Pi to power via the `PWR` port. Find its IP from your router's DHCP table or use the hostname:

```bash
ssh pi@raspberrypi.local
```

Default credentials: user `pi`, password `raspberry`. Change the password immediately.

**4. Clone the repository**

```bash
git clone https://github.com/NicoMancinelli/pi-travel-router.git && cd pi-travel-router
```

**5. Run the installer**

```bash
sudo bash install.sh
```

**6. Answer the interactive prompts**

The installer asks for:
- AP SSID and passphrase (8+ characters)
- Wi-Fi country code (e.g. `US`)
- ntfy.sh topic for push notifications (optional)
- Tailscale auth key (`tskey-auth-...`, optional — you can auth manually later)
- Optional feature flags (DoT, AdGuard Home, kill switch, Tor, blocklists, and more)

All optional features are disabled by default and can be toggled later.

**7. Reboot to activate USB gadget mode**

```bash
sudo reboot
```

The `dwc2`/`g_ether` USB gadget only activates after a reboot.

**8. Connect via USB**

Plug the Pi into your laptop using the `USB` port (not `PWR`). The Pi will appear as a USB Ethernet adapter. SSH in at the fixed gadget address:

```bash
ssh pi@192.168.7.1
```

The Wi-Fi AP (the SSID you configured) is also up at this point.

---

## From Blink Shell on iPhone

Install Blink Shell from the App Store: https://apps.apple.com/app/blink-shell/id1156707581

**Connect via Tailscale (recommended)**

After install, with Tailscale running on both your iPhone and the Pi:

```bash
ssh neek@<tailscale-ip>
```

Replace `neek` with your configured username and `<tailscale-ip>` with the Pi's Tailscale address (shown by `tailscale ip -4`).

**Connect via USB-C hub**

Plug a USB-C hub with a USB-A port into your iPhone. Connect the Pi's `USB` port to the hub. SSH to the gadget address:

```bash
ssh pi@192.168.7.1
```

**Key commands once connected**

```bash
sudo travel-tui        # interactive dashboard: uplink, AP clients, feature flags, logs
sudo travel-status     # one-shot status snapshot
sudo update-router.sh  # pull latest version from GitHub
```

---

## Features

### Core (always on)

| Feature | Notes |
|---|---|
| Wi-Fi AP (hostapd, 802.11n HT40) | `uap0` virtual interface; ~150 Mbps; DTIM=1 for iOS wake latency |
| USB Ethernet gadget (`g_ether`) | Fixed 192.168.7.1/24; laptop connects over USB-C after reboot |
| iPhone USB tethering | udev auto-detect; DHCP fires on plug-in; metric 100 |
| Uplink failover watchdog | 30s timer; tries uplinks in priority order |
| WAN watchdog with graduated recovery | 60s timer; reassociate → restart dhcpcd → reboot |
| Captive portal detection | Probes `generate_204`; pauses/restores Tailscale automatically |
| Tailscale + subnet routing | Advertises `10.3.141.0/24`; remote access over mesh VPN |
| TTL/hop-limit mangling | Sets TTL=65 and IPv6 hop-limit=65 on all uplinks; bypasses hotspot detection |
| DSCP strip | Clears carrier ToS fingerprinting on uplink traffic |
| CAKE qdisc (bufferbloat) | `wlan0` at 50 Mbit; tether at 15 Mbit |
| TCP BBR + FQ qdisc | Better cellular throughput; loaded via `modules-load.d` |
| dnsmasq tuning | cache-size=2048, min-cache-ttl=300, dns-forward-max=300 |
| DNS rebinding protection | `stop-dns-rebind` with local/lan exceptions |
| Client isolation (FORWARD chain) | AP clients cannot reach each other or Pi admin interfaces |
| log2ram | `/var/log` in RAM; protects SD card from write wear |
| CPU performance governor | Eliminates frequency ramp-up latency spikes |
| Hardware watchdog | BCM2835 dtoverlay + `RuntimeWatchdogSec=15`; auto-reboot on kernel lockup |

### Privacy and Security (opt-in)

| Feature | How to enable |
|---|---|
| DNS-over-TLS | `ENABLE_DOT=1` — stubby forwards to Cloudflare + Quad9 on port 5300 |
| VPN kill switch | `ENABLE_VPN_KILLSWITCH=1` — blocks AP traffic when Tailscale drops |
| AdGuard Home | `ENABLE_ADGUARD=1` — replaces dnsmasq DNS; web UI at `:3000`; per-client analytics |
| Tor transparent proxy | `ENABLE_TOR_TRANSPARENT=1` — second SSID (TorAP) routes all TCP through Tor |
| Threat-intel IP blocklist | `ENABLE_BLOCKLISTS=1` — Firehol L1 fetched daily; capped at 20,000 entries for Pi Zero RAM |
| MAC address randomization | Always on for `wlan0` via macchanger systemd service |
| Captive portal MAC clone | `sudo clone-mac.sh <MAC>` — clones laptop MAC to wlan0 before portal auth |

### Connectivity (opt-in)

| Feature | How to enable |
|---|---|
| Bluetooth PAN tethering | Set `IPHONE_BT_MAC` in `/etc/default/travel-router`; metric 300 |
| Android USB tethering | Plug in Android with USB tethering on; udev detects `rndis0`/`usb0`; metric 200 |
| Open Wi-Fi fallback | `ENABLE_OPEN_WIFI_FALLBACK=1` — joins any open network when no other uplink is available |
| Per-device VPN routing | `ENABLE_PER_DEVICE_VPN=1` + `VPN_DEVICE_MACS="..."` — specified MACs routed through Tailscale, others go direct |
| Avahi mDNS reflector | `ENABLE_AVAHI_REFLECTOR=1` — bridges mDNS between `uap0` and `tailscale0` for AirPrint/AirPlay |

### Usability

| Feature | How to enable |
|---|---|
| `travel-tui` interactive dashboard | Always installed; `sudo travel-tui` |
| `travel-status` one-shot status | Always installed; `sudo travel-status` |
| Per-client bandwidth fairness | `ENABLE_CLIENT_QOS=1`; set `AP_CLIENT_BANDWIDTH` for a hard cap |
| Scheduled AP disable | `ENABLE_AP_SCHEDULE=1` — AP off at `AP_DISABLE_TIME` (default 02:00), on at `AP_ENABLE_TIME` (default 07:00) |
| ntfy.sh push notifications | Set `NTFY_TOPIC` in `/etc/default/travel-router` |
| Tailscale peer watchdog | Always on; ntfy alert on daemon down, stale handshake, or peer loss |
| Captive portal auto-login | Per-SSID curl hooks in `/etc/travel-router/portals/<SSID>.sh` |
| Static DHCP leases | Edit `/etc/dnsmasq.d/static-leases.conf` — fill in your MACs |

### Maintenance

| Feature | How to enable |
|---|---|
| Unattended security updates | `ENABLE_AUTO_UPDATES=1` — `unattended-upgrades`; auto-reboot at 03:30 + ntfy notify |
| Auto-update from GitHub | Always installed; `sudo update-router.sh` or weekly systemd timer (Sun 03:00) |
| Log rotation | Always on — daily rotation, 7-day retention, compressed |
| Hardware watchdog | Active after reboot — BCM2835 watchdog via `RuntimeWatchdogSec=15` |

---

## Configuration

The single config file is `/etc/default/travel-router`. It is sourced by all router scripts at runtime.

```bash
# Push notifications
NTFY_TOPIC=""                      # ntfy.sh topic name (treat as a secret)

# Bluetooth tethering
IPHONE_BT_MAC=""                   # iPhone BT MAC, e.g. "AA:BB:CC:DD:EE:FF"

# Tailscale
TAILSCALE_UP_ARGS="--advertise-routes=10.3.141.0/24 --accept-dns=false"

# WAN watchdog probe targets
WAN_PING_TARGETS="1.1.1.1 8.8.8.8"

# Optional feature flags (0 = off, 1 = on)
ENABLE_DOT="0"                     # DNS-over-TLS via stubby
ENABLE_VPN_KILLSWITCH="0"          # Block AP traffic when Tailscale drops
ENABLE_ADGUARD="0"                 # AdGuard Home DNS (web UI at :3000)
ENABLE_BLOCKLISTS="0"              # Firehol L1 IP blocklist
ENABLE_TOR_TRANSPARENT="0"         # Tor transparent proxy on TorAP SSID
ENABLE_HTTP_UA_REWRITE="0"         # HTTP User-Agent normalization (privoxy)
ENABLE_OPEN_WIFI_FALLBACK="0"      # Join any open network as last-resort uplink
ENABLE_AVAHI_REFLECTOR="0"         # mDNS bridge between uap0 and tailscale0
ENABLE_AP_SCHEDULE="0"             # Scheduled AP disable/enable
AP_DISABLE_TIME="02:00"
AP_ENABLE_TIME="07:00"
ENABLE_CLIENT_QOS="0"              # Per-client CAKE bandwidth fairness on uap0
AP_CLIENT_BANDWIDTH="unlimited"    # Hard cap per AP client when QOS is on
ENABLE_PER_DEVICE_VPN="0"          # Route specific MACs through Tailscale
VPN_DEVICE_MACS=""                 # Space-separated MACs for per-device VPN
ENABLE_AUTO_UPDATES="0"            # Unattended security updates

# Blocklist safety cap (Pi Zero has limited RAM)
MAX_BLOCKLIST_ENTRIES="20000"
```

The `travel-tui` Features screen can toggle any `ENABLE_*` flag live without editing the file manually. For flags that affect firewall rules (e.g. `ENABLE_VPN_KILLSWITCH`), the TUI reloads the firewall automatically on toggle.

---

## Optional Features

All optional features are disabled by default. Enable them at install time (the interactive prompts ask about each one) or afterwards by editing `/etc/default/travel-router`.

For features that affect firewall rules — `ENABLE_VPN_KILLSWITCH`, `ENABLE_BLOCKLISTS`, `ENABLE_TOR_TRANSPARENT` — reload the firewall after changing the flag:

```bash
sudo travel-router-firewall.sh --save
```

For service-backed features — `ENABLE_DOT`, `ENABLE_ADGUARD`, `ENABLE_AVAHI_REFLECTOR` — restart the relevant service after enabling:

```bash
sudo systemctl restart stubby          # ENABLE_DOT
sudo systemctl restart adguard-home    # ENABLE_ADGUARD
sudo systemctl restart avahi-daemon    # ENABLE_AVAHI_REFLECTOR
```

---

## Uplink Priority

The failover watchdog checks uplinks in metric order. Lower metric wins:

```
iPhone USB tether  (metric 100)
       |
Android USB tether (metric 200)
       |
Bluetooth PAN      (metric 300)
       |
WiFi STA (wlan0)   (metric 600)
```

The watchdog polls every 30 seconds. When the active uplink loses connectivity, it promotes the next available interface. The WAN watchdog runs independently every 60 seconds and attempts graduated recovery: reassociate → restart dhcpcd → reboot.

---

## Updating

Pull the latest release manually:

```bash
sudo update-router.sh
```

The script checks GitHub releases, falls back to the latest `main` SHA, and re-runs any changed install steps. The install is idempotent — running it again on an already-configured Pi is safe.

Automatic updates run via a weekly systemd timer every Sunday at 03:00. Enable at install time with `ENABLE_AUTO_UPDATES=1`, or enable manually:

```bash
sudo systemctl enable --now travel-router-autoupdate.timer
```

---

## Development

**Linting**

All shell scripts are validated with shellcheck at the warning level:

```bash
shellcheck -S warning scripts/*.sh install.sh
```

**CI**

GitHub Actions runs shellcheck on every push. The workflow file is at `.github/workflows/shellcheck.yml`.

**Idempotency**

The installer is designed to be re-run after changes. It overwrites config files, reinstalls packages, and reloads services without leaving residual state. Running `sudo bash install.sh` on an already-provisioned Pi applies any changes from the repo without requiring a fresh flash.

---

## License

MIT. See `LICENSE`.
