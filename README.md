# pi-travel-router

A self-contained travel router built on a Raspberry Pi Zero 2 W that shares any uplink (iPhone USB, Android USB, Bluetooth PAN, or hotel WiFi) as a private, Tailscale-connected Wi-Fi AP.

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Platform: Pi Zero 2W](https://img.shields.io/badge/platform-Pi%20Zero%202W-c51a4a.svg)
![OS: Pi OS Bookworm](https://img.shields.io/badge/OS-Pi%20OS%20Bookworm-green.svg)

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

Imager handles `xz` decompression and writes the SD card. (CLI alternative: `xz -d travelrouter-*.img.xz && sudo dd if=travelrouter-*.img of=/dev/diskN bs=4M status=progress conv=fsync`.)

**3. Boot the Pi**

Insert the SD card. Connect power to the `PWR` port. Wait ~60 seconds for first boot (mDNS, network, and the firstboot wizard need to come up).

**4. Open the wizard**

Plug the Pi's `USB` port (not `PWR`) into your laptop with a USB-C cable. The image pre-enables USB gadget mode, so the laptop sees a new USB Ethernet device and gets a DHCP lease in `192.168.7.0/24`. Browse to:

```
http://192.168.7.1
```

If the Pi is already on a network you can reach (e.g. via a USB Ethernet hub or pre-seeded Wi-Fi), `http://travelrouter.local` works too. SSH terminal: `ssh root@192.168.7.1` (password `changeme`). Windows users may need RNDIS drivers — see [`build/README.md`](build/README.md) for details.

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

## Day-2 management

```sh
sudo travel-tui          # interactive dashboard: uplink, AP clients, feature toggles, logs
sudo travel-status       # one-shot status snapshot
sudo update-router.sh    # pull latest from GitHub and re-run changed install steps
```

Single config file: `/etc/default/travel-router`. The TUI's Features screen toggles any `ENABLE_*` flag live, reloading the firewall when the change requires it.

---

## Updates

Updates are git-pull based, not image-based:

```sh
sudo update-router.sh
```

The script checks GitHub Releases, falls back to the latest `main` SHA, and re-runs any changed install steps. The installer is idempotent — running it on an already-configured Pi is safe.

A weekly systemd timer (Sun 03:00) does the same thing automatically when `ENABLE_AUTO_UPDATES=1`. New `.img.xz` releases are only cut for major OS bumps (e.g. Bookworm -> Trixie) or to refresh the bootstrap baseline.

---

## Advanced / Manual install

For developers, or to install on an existing Pi OS Lite Bookworm system without re-flashing.

**1. Flash Pi OS Lite Bookworm (64-bit)**

Use Raspberry Pi Imager, select "Raspberry Pi OS Lite (64-bit)". Enable SSH before first boot by creating an empty `ssh` file in the `bootfs` partition:

```sh
touch /Volumes/bootfs/ssh
```

**2. SSH in**

```sh
ssh pi@raspberrypi.local   # default password: raspberry — change immediately
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

`dwc2`/`g_ether` USB gadget mode only activates after a reboot. After it's back, plug the Pi's `USB` port into your laptop and SSH at `192.168.7.1`.

---

## Architecture overview

**Interfaces**

| Interface | Role | Subnet | Metric |
|---|---|---|---|
| `uap0` | Wi-Fi AP (hostapd) | 10.3.141.0/24 | — |
| `usb0` | USB Ethernet gadget (g_ether) | 192.168.7.0/24 | — |
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

Runs as a subnet router advertising `10.3.141.0/24`. AP clients reach your tailnet via the Pi without a Tailscale install. Optional Headscale support points `tailscale up --login-server` at your own VPS.

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

## Project status / roadmap

Full deployed feature list and outstanding roadmap items live in [`IMPROVEMENTS.md`](IMPROVEMENTS.md). Image build pipeline docs: [`build/README.md`](build/README.md). Firstboot wizard internals: [`firstboot/README.md`](firstboot/README.md).

---

## License

MIT. See `LICENSE`.
