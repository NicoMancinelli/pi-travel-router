# Pi Zero Travel Router

An advanced travel router built on a **Raspberry Pi Zero 2 W** optimized for the **Visible wireless network**. Provides a clean, private Wi-Fi hotspot from an iPhone USB tether or any Wi-Fi uplink, with a Tailscale tunnel and TTL manipulation to bypass carrier DPI/hotspot detection.

---

## Architecture

```
iPhone (USB) ──────────────────────────────────────────────────────────┐
                                                                       ▼
Hotel/Cafe Wi-Fi ──► wlan0 (STA) ──► Pi Zero 2 W ──► uap0 (AP) ──► Your Devices
                                          │            10.3.141.1/24
                                          │
                                     tailscale0
                                          │
                                     Exit Node (optional)
                                     bypass Visible DPI
```

**Uplink priority** (configure failover manually or via RaspAP):
1. iPhone USB tether (`enx...` / `eth0`)
2. Wi-Fi STA (`wlan0`)

**Key features:**
- AP/STA concurrent mode — Pi connects upstream and broadcasts its own SSID simultaneously
- TTL=65 mangling — makes all hotspot traffic appear to originate from the phone, bypassing Visible's hotspot throttling/detection
- TCP BBR congestion control — better throughput on cellular links
- Tailscale mesh VPN — remote access to the router from anywhere; optional exit node for encrypted tunnel
- iPhone keepalive — prevents iOS from sleeping the USB tether
- RaspAP web UI — browser-based management at `http://10.3.141.1`

---

## Hardware

| Component | Details |
|---|---|
| Board | Raspberry Pi Zero 2 W |
| Storage | 32GB+ microSD (Class 10 / A1 minimum) |
| Power | USB-C, 5V/2.5A minimum |
| iPhone cable | Lightning or USB-C to USB-A (with USB-A to Micro-USB adapter for Pi Zero) |
| Optional | USB hub if powering Pi from laptop |

---

## Software Stack

| Component | Package / Service | Purpose |
|---|---|---|
| OS | Raspberry Pi OS Lite Bookworm (64-bit) | Base system |
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

1. Flash **Raspberry Pi OS Lite (Bookworm, 32-bit)** to microSD using Raspberry Pi Imager.
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

### Phase 5 — TTL Hack & TCP BBR

**rc.local** (handles TTL mangling, AP interface creation, and power save):

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

# TTL hack for Visible DPI bypass — all outbound traffic appears to come from phone
iptables -t mangle -A POSTROUTING -o uap0 -j TTL --ttl-set 65
iptables -t mangle -A POSTROUTING -o wlan0 -j TTL --ttl-set 65
iptables -t mangle -A POSTROUTING -o eth+ -j TTL --ttl-set 65
iptables -t mangle -A POSTROUTING -o enx+ -j TTL --ttl-set 65

# Disable wlan0 power save (prevents STA disconnects)
iw dev wlan0 set power_save off

exit 0
EOF
sudo chmod +x /etc/rc.local
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

The iPhone suspends the USB tether after ~60s of inactivity. This cron pings Google every minute to keep it alive:

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
ssh -L 8080:10.3.141.1:80 neek@<TAILSCALE_IP>
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

### Tailscale Remote Access

Access the Pi from anywhere on your Tailnet:
```bash
ssh neek@100.105.78.127
# or
ssh neek@travel-router
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
| `100.105.78.127` | Pi — Tailscale IP |
| `10.3.141.0/24` | LAN subnet advertised via Tailscale |

---

## Security Notes

- **Change the RaspAP admin password** from `secret` immediately after install
- **Change the hotspot SSID/password** from the defaults
- The Pi's SSH port is open on the Tailscale IP — keep your Tailscale ACLs tight
- The TTL hack is legal to use on your own devices; it simply prevents artificial throttling
- Tailscale traffic is encrypted end-to-end (WireGuard); Visible cannot inspect it

---

## Potential Improvements

See [IMPROVEMENTS.md](IMPROVEMENTS.md) for a roadmap of enhancements.
