# Potential Improvements

## High Priority

### 1. Automatic iPhone Tether Detection (udev rule)
Currently you must manually enable Personal Hotspot on the iPhone each session. A udev rule can detect when the iPhone is connected and automatically run `dhclient` to acquire an IP over the tether, removing the manual step.

```
# /etc/udev/rules.d/90-ipheth.rules
SUBSYSTEM=="net", ACTION=="add", ATTRS{idVendor}=="05ac", NAME=="enx*", RUN+="/usr/local/bin/start-tether.sh"
```

### 2. Uplink Failover Script
Auto-switch between iPhone USB tether (primary) and Wi-Fi (fallback) based on connectivity. Use `ip route` metric manipulation: iPhone tether gets metric 100, wlan0 gets metric 600. A watchdog script checks internet reachability every 30s and promotes/demotes routes accordingly.

### 3. iptables Persistence via iptables-persistent
The current TTL rules are re-applied at boot via `rc.local`. `iptables-persistent` (already installed) can save/restore rules from `/etc/iptables/rules.v4` — a cleaner, systemd-native approach. Run `sudo netfilter-persistent save` after the rules are active.

### 4. log2ram
The Pi writes logs to the SD card continuously, which shortens card lifespan over time. `log2ram` mounts `/var/log` in RAM and syncs to disk periodically — essential for a device that runs 24/7.

```bash
curl -Lo /tmp/log2ram.deb https://github.com/azlux/log2ram/releases/latest/download/log2ram.deb
sudo dpkg -i /tmp/log2ram.deb
```

### 5. Change Default RaspAP SSID and Credentials
The default SSID `RaspAP` with password `ChangeMe` is a fingerprint. Customize under **Hotspot → Basic** in the web UI, or directly:
```bash
sudo sed -i 's/^ssid=.*/ssid=YourSSID/' /etc/hostapd/hostapd.conf
sudo sed -i 's/^wpa_passphrase=.*/wpa_passphrase=YourPassword/' /etc/hostapd/hostapd.conf
```

---

## Medium Priority

### 6. IPv6 Disable (Leak Prevention)
Visible assigns IPv6 addresses; some DPI systems use IPv6 to fingerprint hotspot traffic independently of TTL. Disabling IPv6 on uplink interfaces closes this leak vector:

```bash
sudo tee -a /etc/sysctl.conf <<EOF
net.ipv6.conf.wlan0.disable_ipv6 = 1
net.ipv6.conf.eth0.disable_ipv6 = 1
EOF
```

### 7. DNS-over-HTTPS (dnscrypt-proxy)
Replace Quad9/Cloudflare with `dnscrypt-proxy` to encrypt DNS queries. This prevents your ISP (Visible/Verizon) from seeing DNS lookups, and avoids DNS-based hotspot detection used by some carriers.

```bash
sudo apt install -y dnscrypt-proxy
```

Configure `/etc/dnscrypt-proxy/dnscrypt-proxy.toml` with your preferred resolvers, then point dnsmasq at `127.0.0.1:5300`.

### 8. MAC Address Randomization for wlan0
When connecting to hotel/airport Wi-Fi, use a randomized MAC to avoid tracking:

```bash
# /etc/NetworkManager/conf.d/wifi-mac-random.conf  (if using NetworkManager)
# or add to wpa_supplicant config:
mac_addr=1  # randomize on each association
```

With dhcpcd, add to `/etc/dhcpcd.conf`:
```
interface wlan0
clientid
```

### 9. Tailscale Killswitch
If Tailscale is being used as an exit node and drops, traffic will fall back to the carrier unencrypted. A killswitch blocks all non-Tailscale internet traffic when the tunnel is down:

```bash
sudo tailscale up \
  --advertise-routes=10.3.141.0/24 \
  --exit-node=<IP> \
  --exit-node-allow-lan-access=true \
  --accept-dns=false
```

For a hard iptables killswitch, block all `FORWARD` traffic except on `tailscale0` and `uap0`.

### 10. Bandwidth Monitoring Dashboard
`vnstat` is installed but only CLI. Add `vnstati` for graph generation, or expose vnstat data via a lightweight web endpoint accessible from RaspAP. Alternatively, install **Grafana + Prometheus node_exporter** for a full metrics dashboard (requires more RAM — borderline on Pi Zero 2 W).

---

## Low Priority / Advanced

### 11. WireGuard DIY Tunnel (replace Tailscale)
If you want full control over the VPN without a third-party coordination server, replace Tailscale with a self-hosted WireGuard setup:
```bash
sudo apt install -y wireguard
```
Requires a VPS with a static IP as the WireGuard server. More complex to set up but zero external dependency.

### 12. Per-Client Bandwidth Throttling
Use `tc` (traffic control) to rate-limit individual clients — useful when sharing with others in a hotel:
```bash
# Limit client at 10.3.141.50 to 5Mbps down
sudo tc qdisc add dev uap0 root handle 1: htb default 10
sudo tc class add dev uap0 parent 1: classid 1:1 htb rate 5mbit
sudo tc filter add dev uap0 parent 1: protocol ip u32 match ip dst 10.3.141.50/32 flowid 1:1
```

### 13. Captive Portal Auto-Login
Hotels often use captive portals. A script using `curl` can detect and auto-submit portal login forms for known hotel chains. Requires per-network customization.

### 14. Static DHCP Leases for Your Devices
Assign fixed IPs to your known devices via dnsmasq for predictable addressing:
```
# /etc/dnsmasq.d/static-leases.conf
dhcp-host=AA:BB:CC:DD:EE:FF,10.3.141.10,macbook
dhcp-host=11:22:33:44:55:66,10.3.141.11,ipad
```

### 15. Read-Only Root Filesystem (overlayfs)
For a bulletproof travel deployment, mount the root filesystem read-only with an overlayfs. Prevents SD card corruption from sudden power loss (common when disconnecting from a laptop USB port).

Enable via `raspi-config` → **Performance Options → Overlay File System**.
