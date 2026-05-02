# Pi Zero Travel Router

An advanced travel router built on a **Raspberry Pi Zero 2 W** optimized for the **Visible wireless network**. Provides a clean, private Wi-Fi hotspot from an iPhone USB tether or any Wi-Fi uplink — with USB Ethernet gadget mode for direct laptop connection, Tailscale VPN, TTL mangling to bypass carrier DPI, captive portal auto-handling, and push notifications via ntfy.sh.

---

## Architecture

```
                    ┌─────────────────────────────────────────────────────────┐
iPhone USB ─────────►  enx* (ipheth, metric 100)  ─────────────────────────┐ │
                    │                                                        │ │
Hotel/Cafe WiFi ────►  wlan0 (STA, metric 600)     ──► NAT/routing ──► uap0 (AP) ──► WiFi clients
                    │                                       │         10.3.141.1/24   │
                    │  Open WiFi fallback ──────────────────┘                        │
                    │                                                                 │
Laptop USB-C ───────►  usb0 (g_ether gadget)  ─────────────────────────────────────┘
                    │  192.168.7.1/24                                                 │
                    │                                                                 │
                    │  tailscale0 ──► Tailnet / Exit Node (optional DPI bypass)      │
                    └─────────────────────────────────────────────────────────────────┘
```

**Uplink priority** (automatic, 30s failover watchdog):
1. iPhone USB tether (`enx*`) — metric 100, preferred
2. Bluetooth PAN (`bnep0`) — metric 300, low-bandwidth fallback
3. Wi-Fi STA (`wlan0`) — metric 600, fallback
4. Optional Open WiFi fallback — disabled by default; catches any open network if enabled

**Client connections:**
- Wi-Fi AP (`uap0`) — `10.3.141.1/24` — any device on your SSID
- USB direct (`usb0`) — `192.168.7.1/24` — laptop via USB-C → micro-USB cable

**Key features:**
| Feature | Details |
|---|---|
| AP/STA concurrent mode | Pi connects upstream and broadcasts own SSID simultaneously |
| TTL=65 + IPv6 hop-limit=65 | Both IPv4 and IPv6 traffic appears to come from phone — bypasses Visible DPI |
| TCP BBR + CAKE qdisc | BBR handles sender congestion; CAKE eliminates bufferbloat on egress |
| 802.11n + DTIM=1 | HT40, WMM enabled; DTIM=1 halves iOS client wake latency |
| CPU performance governor | Eliminates ramp-up latency spikes during packet forwarding bursts |
| USB Ethernet Gadget | Laptop gets internet via USB without using Wi-Fi — `192.168.7.1/24` |
| Auto iPhone tether | udev detects plug-in, runs dhclient + CAKE automatically |
| Uplink failover watchdog | 30s; promotes tether, demotes WiFi, falls back on failure |
| WAN watchdog + recovery | 60s; reassociate → restart dhcpcd → restart networking → reboot |
| Captive portal detection | Probes generate_204; auto-pauses/restores Tailscale for portal auth |
| Open WiFi fallback | Optional; disabled by default in `/etc/default/travel-router` |
| MAC randomization | NetworkManager + macchanger randomize wlan0 MAC on each connection |
| Client isolation | AP clients can't reach each other or Pi admin interfaces |
| Tailscale VPN | Remote access from anywhere; optional exit node; subnet routing |
| ntfy.sh notifications | Push alerts for WAN events, tether connect/disconnect, captive portals |
| log2ram | `/var/log` in RAM → SD card not worn by continuous log writes |

---

## Hardware

| Component | Details |
|---|---|
| Board | Raspberry Pi Zero 2 W |
| Storage | 32GB+ microSD (Class 10 / A1 minimum) |
| Power | 5V/2.5A via micro-USB power port |
| iPhone cable | Lightning/USB-C → USB-A → micro-USB OTG adapter → Pi OTG port |
| Laptop connection | USB-C → micro-USB OTG (Pi appears as USB Ethernet adapter) |
| Optional | USB hub + OTG adapter if using iPhone USB tether AND laptop USB simultaneously |

> **Note:** The Pi Zero 2 W has one micro-USB OTG port. iPhone USB tethering (Pi as USB host) and USB Ethernet Gadget (Pi as USB device) are mutually exclusive on this port. Use a USB hub with an OTG adapter to run both simultaneously, or tether iPhone via WiFi hotspot when using USB gadget mode.

---

## Software Stack

| Component | Package / Service | Purpose |
|---|---|---|
| OS | Raspberry Pi OS Lite Bookworm (32-bit or 64-bit) | Base system |
| AP daemon | `hostapd` | Broadcasts Wi-Fi hotspot on `uap0` |
| DHCP/DNS | `dnsmasq` | Assigns IPs to clients, local DNS |
| Web UI | `lighttpd` + RaspAP | Browser-based router management |
| VPN mesh | `tailscale` | Remote access + optional exit node |
| iOS tether | `usbmuxd`, `libimobiledevice`, `ipheth-utils` | iPhone USB tethering |
| Monitoring | `vnstat` | Bandwidth usage tracking |
| Firewall | `iptables` + `iptables-persistent` | TTL mangling, NAT |

---

## Initial Setup (from scratch)

### Phase 1 — OS & Base Config

1. Flash **Raspberry Pi OS Lite Bookworm (32-bit or 64-bit)** to microSD using Raspberry Pi Imager.
2. In Imager's advanced settings, set:
   - Hostname: `travel-router`
   - SSH: enabled
   - Username: `neek` (or your choice)
   - Password: (set a strong password)
   - Wi-Fi: your home network (for initial setup)
   - Locale/timezone: your region
3. Boot the Pi and SSH in:
   ```bash
   ssh neek@travel-router.local
   ```
4. Update the system:
   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo apt install -y git curl
   ```

### Phase 2 — iOS Tethering Dependencies

```bash
sudo apt install -y usbmuxd libimobiledevice6 libimobiledevice-utils ipheth-utils vnstat
```

Enable and start usbmuxd:
```bash
sudo systemctl enable usbmuxd
sudo systemctl start usbmuxd
```

### Phase 3 — RaspAP

Install RaspAP in non-interactive mode:
```bash
curl -sL https://install.raspap.com | bash -s -- --yes
```

This installs and configures: `lighttpd`, `hostapd`, `dnsmasq`, PHP, and sets up the web UI. The installer enables AP/STA concurrent mode automatically on Pi hardware.

After install, reboot:
```bash
sudo reboot
```

**Post-install — change default credentials immediately:**

Access the web UI at `http://10.3.141.1` (connect to the `RaspAP` SSID first, or access via SSH tunnel).

Default RaspAP login:
- Username: `admin`
- Password: `secret`

Change in **Authentication → Change Password**.

Then under **Hotspot → Basic**:
- Change SSID from `RaspAP` to something inconspicuous
- Set a strong WPA2 password
- Save & restart hotspot

### Phase 4 — Tailscale

Install:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

Enable IP forwarding:
```bash
sudo tee /etc/sysctl.d/99-tailscale.conf <<EOF
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF
sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
```

Authenticate (advertise the RaspAP subnet so your other Tailscale devices can reach clients):
```bash
sudo tailscale up --advertise-routes=10.3.141.0/24 --accept-dns=false
```

Open the authentication URL in a browser. Then in the **Tailscale admin console**:
- Approve the advertised route `10.3.141.0/24`
- Optionally: set an exit node if you want all Pi traffic to egress through a trusted server

**Optional — use an exit node to bypass Visible DPI:**
```bash
sudo tailscale up \
  --advertise-routes=10.3.141.0/24 \
  --exit-node=<TAILSCALE_IP_OF_EXIT_NODE> \
  --accept-dns=false
```

Your exit node (home server, VPS) must have `--advertise-exit-node` set and be approved in the admin console.

### Phase 5 — AP Interface, Firewall & TCP BBR

**rc.local** (handles AP interface creation, channel sync, and power save):

```bash
sudo tee /etc/rc.local <<'EOF'
#!/bin/bash

# Wait for wlan0 to associate, then match hostapd channel to it
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    CHAN=$(iw dev wlan0 info 2>/dev/null | awk '/channel/{print $2}')
    [ -n "$CHAN" ] && break
    sleep 1
done
[ -n "$CHAN" ] && sed -i "s/^channel=.*/channel=$CHAN/" /etc/hostapd/hostapd.conf

# Create virtual AP interface for concurrent AP+STA mode
iw dev wlan0 interface add uap0 type __ap 2>/dev/null || true
ip link set uap0 up
ip addr add 10.3.141.1/24 dev uap0 2>/dev/null || true

# Restart hostapd with correct channel
systemctl restart hostapd

# Disable wlan0 power save (prevents STA disconnects)
iw dev wlan0 set power_save off

exit 0
EOF
sudo chmod +x /etc/rc.local
```

Firewall, TTL, DSCP, and optional proxy rules are applied idempotently by:
```bash
sudo install -m 755 scripts/travel-router-firewall.sh /usr/local/bin/travel-router-firewall.sh
sudo /usr/local/bin/travel-router-firewall.sh --save
```

**TCP BBR** (better throughput on high-latency cellular):
```bash
# Load module now and persist at boot
sudo modprobe tcp_bbr
echo tcp_bbr | sudo tee /etc/modules-load.d/tcp_bbr.conf

# Add to sysctl
echo "net.core.default_qdisc=fq" | sudo tee -a /etc/sysctl.conf
echo "net.ipv4.tcp_congestion_control=bbr" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### Phase 6 — iPhone Anti-Sleep Keepalive

Legacy installs used a cron keepalive. Current installs use `wan-watchdog.timer` instead, so do not add this cron on new systems unless you intentionally want a simple fallback:

```bash
sudo tee /usr/local/bin/keepalive.sh <<'EOF'
#!/bin/bash
ping -c 1 8.8.8.8 > /dev/null 2>&1
EOF
sudo chmod +x /usr/local/bin/keepalive.sh

# Add to root crontab
(sudo crontab -l 2>/dev/null; echo "* * * * * /usr/local/bin/keepalive.sh") | sudo crontab -
```

---

## Daily Use

### Connecting Devices

1. On your laptop/tablet: connect to your custom SSID (set in RaspAP)
2. Default gateway will be `10.3.141.1`
3. Internet routes through whatever uplink the Pi is using

### Switching Uplinks

**iPhone USB tether (Visible):**
1. Plug iPhone into Pi via USB
2. On iPhone: **Settings → Personal Hotspot → Allow Others to Join** (toggle on)
3. Trust the computer if prompted
4. The `enx...` interface will appear; RaspAP handles routing automatically
5. Verify: `ip addr show` should show an `enx...` interface with a 172.x.x.x address

**Wi-Fi uplink:**
- Managed via RaspAP web UI → **Wireless Client** tab
- Or via `wpa_supplicant` directly:
  ```bash
  sudo wpa_cli -i wlan0 add_network
  sudo wpa_cli -i wlan0 set_network 0 ssid '"NetworkName"'
  sudo wpa_cli -i wlan0 set_network 0 psk '"Password"'
  sudo wpa_cli -i wlan0 enable_network 0
  ```

### RaspAP Web UI

Access at `http://10.3.141.1` from any device connected to the Pi's hotspot, or via SSH tunnel:
```bash
ssh -L 8080:10.3.141.1:80 <pi-user>@<TAILSCALE_IP>
# Then open http://localhost:8080
```

| Section | What to configure |
|---|---|
| Dashboard | Live interface stats, client count |
| Hotspot | SSID, password, channel, band |
| Wireless Client | Which Wi-Fi network wlan0 connects to |
| DHCP Server | IP ranges, lease time, static assignments |
| Ad Blocking | Optional DNS-based ad/tracker blocking |
| System | RaspAP updates, password change |

### USB Ethernet Gadget (Laptop Direct Connection)

Plug the Pi into your laptop via USB-C → micro-USB (OTG port). After the Pi boots, your laptop sees a USB Ethernet adapter and gets an IP automatically:

| Address | Role |
|---|---|
| `192.168.7.1` | Pi (gateway) |
| `192.168.7.2–100` | DHCP pool (your laptop) |

**macOS:** A new "RNDIS/Ethernet Gadget" adapter appears in Network Preferences. It auto-configures via DHCP. No drivers needed.

**Windows:** May require the RNDIS driver (built into Windows 10/11). Check Device Manager if the adapter doesn't appear.

**Linux:** The interface appears as `enp0s...` or `usb0`. Run `sudo dhclient usb0` if it doesn't auto-configure.

**Requires a reboot after initial setup** to activate the `dwc2` kernel overlay and load the `g_ether` module.

> **iPhone USB tether and USB gadget are mutually exclusive** on the Pi Zero 2 W's single OTG port. When using USB gadget for the laptop, tether your iPhone via WiFi hotspot instead (iPhone broadcasts, Pi connects as STA on wlan0).

### Tailscale Remote Access

Access the Pi from anywhere on your Tailnet:
```bash
ssh <pi-user>@<TAILSCALE_IP>
# or
ssh <pi-user>@travel-router
```

Access a device connected *to* the Pi's hotspot (after approving the `10.3.141.0/24` route):
```bash
ssh user@10.3.141.<X>
```

Check Tailscale status:
```bash
sudo tailscale status
sudo tailscale ping <node-name>
```

### Push Notifications (ntfy.sh)

1. Pick a unique topic name — treat it as a secret: `travel-router-yourname-abc123`
2. Install the **ntfy** app on iOS/Android and subscribe to your topic
3. On the Pi: `sudo nano /etc/default/travel-router` → set `NTFY_TOPIC="your-topic"`

You'll receive notifications for: WAN up/down, captive portal detected, iPhone tether connect/disconnect, router reboots.

Test: `sudo /usr/local/bin/notify-router.sh "test" default`

### Bandwidth Monitoring (vnstat)

```bash
# Current session
vnstat -l

# Daily summary
vnstat -d

# Monthly
vnstat -m

# Per interface
vnstat -i enx0000000000  # replace with actual tether interface name
```

---

## Verification Checklist

After every reboot, confirm:

```bash
# 1. Services running
systemctl is-active lighttpd hostapd dnsmasq tailscaled

# 2. AP interface up with correct IP
ip addr show uap0

# 3. TTL rules applied
sudo iptables -t mangle -L POSTROUTING --line-numbers | grep TTL

# 4. BBR active
sysctl net.ipv4.tcp_congestion_control net.core.default_qdisc

# 5. Tailscale connected
sudo tailscale status | head -3

# 6. IP forwarding on
sysctl net.ipv4.ip_forward
```

---

## Troubleshooting

### No internet on connected devices

```bash
# Check uplink
ip route show
ping -c 3 8.8.8.8

# Check NAT masquerade rule (RaspAP sets this)
sudo iptables -t nat -L POSTROUTING -n -v

# Check dnsmasq
sudo systemctl status dnsmasq
sudo journalctl -u dnsmasq -n 30
```

### iPhone tether interface not appearing

```bash
# Check usbmuxd is running
systemctl status usbmuxd

# List connected iOS devices
idevice_id -l

# Check kernel sees the device
dmesg | tail -20 | grep -i usb

# Manually bring up the interface (replace enx... with actual name)
sudo dhclient enx000000000000
```

The iPhone must have **Personal Hotspot enabled** and have **trusted this computer**. If not trusted, a dialog appears on the phone.

### hostapd not starting / stuck in "activating"

```bash
sudo systemctl status hostapd
sudo journalctl -u hostapd -n 50

# Check hostapd config syntax
sudo hostapd -d /etc/hostapd/hostapd.conf

# Verify uap0 interface exists
ip link show uap0

# If uap0 is missing, recreate it:
sudo iw dev wlan0 interface add uap0 type __ap
sudo ip link set uap0 up
sudo ip addr add 10.3.141.1/24 dev uap0
sudo systemctl restart hostapd
```

### Tailscale not connecting

```bash
sudo tailscale status
sudo journalctl -u tailscaled -n 30

# Re-authenticate if needed
sudo tailscale up --advertise-routes=10.3.141.0/24 --accept-dns=false
```

### rc.local not running on boot

```bash
sudo systemctl status rc-local
sudo bash -n /etc/rc.local  # check for syntax errors
sudo systemctl restart rc-local
```

### BBR not active after reboot

```bash
# Check if module is loaded
lsmod | grep bbr

# Load manually
sudo modprobe tcp_bbr
sudo sysctl -w net.ipv4.tcp_congestion_control=bbr
sudo sysctl -w net.core.default_qdisc=fq

# Verify module loads at boot
cat /etc/modules-load.d/tcp_bbr.conf  # should contain: tcp_bbr
```

---

## Network Reference

| Address | Description |
|---|---|
| `10.3.141.1` | Pi — AP gateway, RaspAP web UI |
| `10.3.141.2–254` | DHCP pool — connected client devices |
| `<TAILSCALE_IP>` | Pi — Tailscale IP |
| `10.3.141.0/24` | LAN subnet advertised via Tailscale |

---

## Security Notes

- **Change the RaspAP admin password** from `secret` immediately after install
- **Change the hotspot SSID/password** from the defaults
- Never commit live Pi passwords, Tailscale auth keys, ntfy topics, or private host details. If one lands in git, rotate it outside the repo before continuing.
- The Pi's SSH port is open on the Tailscale IP — keep your Tailscale ACLs tight
- The TTL hack is legal to use on your own devices; it simply prevents artificial throttling
- Tailscale traffic is encrypted end-to-end (WireGuard); Visible cannot inspect it

---

## Repository Structure

```
├── scripts/
│   ├── start-tether.sh        # udev: iPhone plug-in → dhclient + CAKE + ntfy
│   ├── stop-tether.sh         # udev: iPhone unplug → release DHCP + ntfy
│   ├── failover-watchdog.sh   # systemd: uplink metrics every 30s
│   ├── wan-watchdog.sh        # systemd: WAN health + graduated recovery every 60s
│   ├── captive-check.sh       # called by wan-watchdog: portal detect + Tailscale pause
│   ├── notify-router.sh       # ntfy.sh push notification wrapper
│   ├── apply-cake.sh          # CAKE qdisc on uplinks (also called at boot)
│   ├── travel-router-firewall.sh  # idempotent firewall / TTL / optional proxy rules
│   ├── start-bt-tether.sh     # Bluetooth PAN tethering
│   ├── stop-bt-tether.sh      # Bluetooth PAN teardown
│   ├── update-blocklists.sh   # optional nftables blocklist updater
│   ├── vnstat-metrics.sh      # Prometheus textfile exporter
│   └── keepalive.sh           # legacy, not installed by default
├── config/
│   ├── rc.local                          # AP interface, channel sync, power save
│   ├── 90-ipheth.rules                   # udev: systemd tether@.service trigger
│   ├── 99-apple-autosuspend.rules        # udev: disable Apple USB autosuspend
│   ├── 99-tailscale.conf                 # sysctl: IP forwarding
│   ├── 99-disable-ipv6-uplink.conf       # sysctl: IPv6 off on uplinks
│   ├── tcp-bbr.conf                      # sysctl: BBR + FQ
│   ├── hostapd.conf                      # reference: 802.11n/DTIM config
│   ├── dnsmasq-travel-tweaks.conf        # DNS cache, min-TTL, query slots
│   ├── dnsmasq-usb-gadget.conf           # DHCP for USB gadget (usb0)
│   ├── dnsmasq-static-leases.conf        # template: assign fixed IPs to devices
│   ├── dnsmasq-tor-ap.conf               # optional Tor AP DHCP config
│   ├── privoxy-user.action               # optional HTTP UA rewrite action
│   ├── travel-router-defaults            # /etc/default/travel-router config
│   ├── wpa_supplicant-open-fallback.conf # optional open WiFi catch-all
│   └── NetworkManager-wifi-random-mac.conf  # MAC randomization
└── systemd/
    ├── failover-watchdog.service / .timer  # uplink metric management (30s)
    ├── wan-watchdog.service / .timer       # WAN health + recovery (60s)
    ├── tether@.service                     # udev-triggered USB tether setup
    ├── update-blocklists.service / .timer  # optional nftables blocklist refresh
    ├── vnstat-metrics.service / .timer     # bandwidth metrics export
    ├── cpu-performance.service             # performance CPU governor at boot
    ├── wlan-mac-random.service             # randomize wlan0 MAC at boot
    └── cake-qdisc.service                  # apply CAKE on wlan0 at boot
```

## Potential Improvements

See [IMPROVEMENTS.md](IMPROVEMENTS.md) for a full roadmap with GitHub-sourced tweaks, prioritized by impact.
