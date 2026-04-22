# Improvements & Roadmap

Items marked ✅ are deployed. The rest are future candidates, ordered by impact within each category.

---

## Deployed Features

| ✅ | Feature | Notes |
|---|---|---|
| ✅ | RaspAP (lighttpd, hostapd, dnsmasq) | AP/STA concurrent mode on uap0 |
| ✅ | iOS USB tether auto-detect (udev) | Plug in iPhone → DHCP fires automatically |
| ✅ | Uplink failover watchdog | 30s timer; tether metric 100, wlan0 metric 600 |
| ✅ | WAN watchdog with graduated recovery | 60s timer; reassociate → restart dhcpcd → reboot |
| ✅ | Captive portal detection + Tailscale pause | Probes generate_204; pauses/restores Tailscale automatically |
| ✅ | ntfy.sh push notification framework | Set `NTFY_TOPIC` in `/etc/default/travel-router` |
| ✅ | USB Ethernet Gadget (g_ether) | Laptop via USB-C → micro-USB; 192.168.7.1/24 **[needs reboot]** |
| ✅ | Open WiFi fallback | wpa_supplicant connects to any open network (priority=1) |
| ✅ | Tailscale + subnet routing (10.3.141.0/24) | Remote access over mesh VPN |
| ✅ | IP forwarding (IPv4 + IPv6) | `/etc/sysctl.d/99-tailscale.conf` |
| ✅ | TTL=65 iptables mangling | uap0, wlan0, eth+, enx+ — bypasses Visible hotspot detection |
| ✅ | IPv6 hop-limit=65 (ip6tables) | Mirrors TTL mangling for IPv6 traffic |
| ✅ | IPv6 disabled on uplink interfaces | Closes DPI fingerprinting vector TTL alone doesn't cover |
| ✅ | TCP BBR + FQ qdisc | Better cellular throughput; persists via modules-load.d |
| ✅ | CAKE qdisc for bufferbloat | wlan0 at 50mbit; tether at 15mbit via start-tether.sh |
| ✅ | CPU performance governor | Systemd oneshot; eliminates ramp-up latency spikes |
| ✅ | hostapd 802.11n (HT40, WMM, DTIM=1) | ~150 Mbps AP; DTIM=1 halves iOS client wake latency |
| ✅ | dnsmasq tuning | cache-size=2048, min-cache-ttl=300, dns-forward-max=300 |
| ✅ | FORWARD chain client isolation | AP clients can't reach each other or Pi admin interfaces |
| ✅ | INPUT: block AP clients from port 80/22 | Manage only via Tailscale or USB gadget (192.168.7.x) |
| ✅ | MAC address randomization (wlan0) | NetworkManager + macchanger systemd service |
| ✅ | log2ram | `/var/log` in RAM; protects SD card (active after reboot) |
| ✅ | iptables-persistent save | TTL + firewall rules in `/etc/iptables/rules.v{4,6}` |
| ✅ | iPhone keepalive (replaced by WAN watchdog) | Old cron removed; watchdog covers this and more |
| ✅ | Static DHCP leases template | `/etc/dnsmasq.d/static-leases.conf` — fill in your MACs |

---

## Roadmap — Original 15 (From GitHub Research)

### 🔴 High Priority

#### 1. nftables TTL Migration
Replace `iptables -t mangle` TTL rules with native nftables, which also handles IPv6 hop-limit in one ruleset. On Pi OS Bookworm, `iptables-nft` already uses the nftables kernel backend, so this is a syntax/management improvement rather than a backend change. Key benefit: single ruleset covers both IPv4 TTL and IPv6 hop-limit.

```bash
# /etc/nftables.conf addition
table inet mangle {
    chain postrouting {
        type filter hook postrouting priority mangle;
        oifname { "uap0", "wlan0", "enx*", "usb0" } ip ttl set 65
        oifname { "uap0", "wlan0", "enx*", "usb0" } ip6 hop-limit set 65
    }
}
```
Disable `iptables-persistent` for mangle after migrating.

#### 2. Captive Portal MAC Cloning
Before connecting to hotel WiFi, clone your laptop's MAC to wlan0 so the portal only sees one authenticated device. Combined with the existing captive portal detection, this fully automates hotel onboarding.

```bash
# /usr/local/bin/clone-mac.sh
ip link set wlan0 down
ip link set wlan0 address <laptop-mac>
ip link set wlan0 up
nmcli device connect wlan0
```

#### 3. hostapd HT Capability (full review)
Current config uses `[HT40][SHORT-GI-20][DSSS_CCK-40]`. The `[SHORT-GI-40]` capability was tried and rejected by the brcmfmac driver. Consider testing `[HT40+]` vs `[HT40-]` depending on channel — `+` means secondary channel above primary, `-` means below. Channel 6 with `[HT40-]` uses channels 2+6 which is cleaner in most deployments.

---

### 🟡 Medium Priority

#### 4. CAKE Bandwidth Auto-Tuning
Current CAKE config uses hardcoded 50mbit/15mbit. Auto-detect actual uplink speed via a periodic `speedtest-cli` or `fast-cli` run and adjust CAKE bandwidth accordingly.

```bash
SPEED=$(speedtest-cli --simple 2>/dev/null | awk '/Upload/{print int($2 * 0.9) "mbit"}')
[ -n "$SPEED" ] && tc qdisc replace dev wlan0 root cake bandwidth "$SPEED" besteffort
```

#### 5. Android USB Tethering (RNDIS)
Extend the udev auto-tether rules to cover Android phones (vendor IDs vary; use `rndis_host` kernel module). Add `usbcore.autosuspend=-1` to `/boot/firmware/cmdline.txt` to prevent USB autosuspend breaking RNDIS.

```
# /etc/udev/rules.d/91-android-tether.rules
SUBSYSTEM=="net", ACTION=="add", KERNEL=="usb0", RUN+="/usr/local/bin/start-tether.sh %k"
SUBSYSTEM=="net", ACTION=="add", KERNEL=="rndis0", RUN+="/usr/local/bin/start-tether.sh %k"
```

#### 6. WireGuard Split Tunnel (IP/CIDR-based)
Route specific subnets (corporate, banking) through Tailscale while streaming/general traffic goes direct. Implement via `ip rule` + fwmark + secondary routing table.

```bash
ip rule add fwmark 0x1 table 200
ip route add default via 100.x.x.x dev tailscale0 table 200
iptables -t mangle -A PREROUTING -s 10.3.141.20 -j MARK --set-mark 0x1  # specific client
```

#### 7. iptables FORWARD — Full Stateful Policy
Current FORWARD policy is ACCEPT (for RaspAP compatibility). A cleaner approach once RaspAP is stable: set `FORWARD DROP`, enumerate allowed flows explicitly.

```bash
iptables -P FORWARD DROP
iptables -A FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A FORWARD -i uap0 -o wlan0 -j ACCEPT
iptables -A FORWARD -i uap0 -o enx+ -j ACCEPT
iptables -A FORWARD -i usb0 -o wlan0 -j ACCEPT
```

#### 8. DNS-over-TLS (stubby)
Encrypt upstream DNS queries so Visible/Verizon can't see or inject DNS responses.

```bash
sudo apt install -y stubby
# Configure /etc/stubby/stubby.yml with Cloudflare/Quad9 DoT servers
# Point dnsmasq at 127.0.0.1:5300 (stubby's listening port)
```

---

### 🟢 Low Priority

#### 9. Selective Tailscale Routing by Client MAC
Route specific devices through Tailscale exit node while others go direct.

#### 10. Read-Only Root Filesystem (overlayfs)
Prevents SD card corruption from sudden power loss. Enable via `raspi-config → Performance Options → Overlay File System`. Disable before any system updates.

#### 11. Scheduled SSID Disable
Disable the AP at night to reduce attack surface and RF exposure.

```bash
# /etc/systemd/system/ap-sleep.timer
# 02:00 disable, 07:00 re-enable
```

#### 12. Avahi mDNS Reflector
Bridge mDNS between uap0 and tailscale0 so AP clients can discover home devices (AirPrint, AirPlay, NAS).

```
# /etc/avahi/avahi-daemon.conf
[reflector]
enable-reflector=yes
```

#### 13. DNS-over-HTTPS (dnscrypt-proxy)
Alternative to stubby; supports server rotation and anonymized DNS.

#### 14. Static DHCP Leases for Your Devices
Fill in `/etc/dnsmasq.d/static-leases.conf` with your devices' MACs for predictable IPs.

#### 15. Automatic Security Updates
```bash
sudo apt install -y unattended-upgrades
# Configure /etc/apt/apt.conf.d/50unattended-upgrades
# Add reboot window via systemd timer + ntfy.sh pre-reboot notification
```

---

## Roadmap — 20 Additional Features (From GL.iNet / OpenWrt Research)

### Security

| # | Feature | Complexity | Package/Tool |
|---|---|---|---|
| 16 | **Encrypted DNS (DoT/DoH)** — stubby or dnscrypt-proxy2 between dnsmasq and upstream; closes the biggest daily privacy gap | Low | `stubby` / `dnscrypt-proxy` |
| 17 | **VPN Kill Switch** — nftables policy-drop, only VPN traffic passes; fail-safe instead of fail-open when Tailscale drops | Low-Med | wg-killswitch-nft pattern |
| 18 | **AdGuard Home** — replaces dnsmasq for DNS; per-client query history, 15M+ domain blocklist, DoT/DoH upstream, web UI; GL.iNet flagship feature | Medium | `adguard-home` binary (ARM) |
| 19 | **Admin TOTP 2FA** — TOTP challenge on RaspAP lighttpd login; protects UI from hostile-network credential attacks | Medium | `libpam-oath` + `oathtool` |
| 20 | **Transparent Tor Proxy** — iptables redirect all TCP through Tor TransPort; clients browse Tor without configuration; for high-risk travel | High | `tor` + iptables redirect rules |

### Performance

| # | Feature | Complexity | Package/Tool |
|---|---|---|---|
| 21 | **Per-Client Bandwidth Caps** — tc HTB + nftables marks to rate-limit individual devices; prevents one device from saturating shared uplink | Medium | `tc htb` + nftables marks |
| 22 | **WireGuard + Domain-Based Split Tunnel** — dnsmasq ipset + ip rule fwmark routes specific domains through Tailscale, rest go direct | Med-High | `ipset` + `wireguard-tools` |
| 23 | **Captive Portal Auto-Login (travelmate pattern)** — polls for known SSIDs, detects portal, runs pre-configured curl login scripts; full automation | Medium | Custom bash + `curl` |

### Reliability

| # | Feature | Complexity | Package/Tool |
|---|---|---|---|
| 24 | **Android USB Tethering (RNDIS/CDC-ECM)** — udev rules for Android phones; genuine second-carrier redundancy | Low-Med | `rndis_host` kernel module + udev |
| 25 | **UPS HAT / Safe Shutdown** — Waveshare or PiPower 5 UPS HAT; I2C battery monitor triggers `shutdown` before depletion; eliminates SD corruption risk | Low (sw) | Waveshare UPS HAT + Python |
| 26 | **Unattended Security Updates** — auto-apply security patches nightly; reboot window + ntfy.sh pre-reboot notification | Low | `unattended-upgrades` |
| 27 | **WAN Metric Auto-Management (ifmetric)** — enforce consistent interface metrics as uplinks come and go; prevents silent wrong-path routing | Low-Med | `ifmetric` / systemd-networkd |

### Usability

| # | Feature | Complexity | Package/Tool |
|---|---|---|---|
| 28 | **mDNS Bridging (Avahi Reflector)** — bridge mDNS between uap0 and tailscale0; unlocks AirPrint, AirPlay, NAS discovery across Tailscale | Low | `avahi-daemon` (already installed) |
| 29 | **Scheduled SSID Disable** — hostapd_cli disable/enable on timer; reduces attack surface and RF during sleep | Low | systemd timer + `hostapd_cli` |
| 30 | **USB Drive File Sharing** — plug USB drive into Pi (via hub); expose via Samba or SFTP to AP clients; travel NAS mode | Medium | `samba` / `minidlna` / `vsftpd` |
| 31 | **Captive Portal MAC Clone** — clone laptop MAC to wlan0 before portal auth; hotel sees one device for all connected clients | Low | `macchanger` (already installed) |

### Monitoring & Analytics

| # | Feature | Complexity | Package/Tool |
|---|---|---|---|
| 32 | **Bandwidth Analytics Dashboard** — vnStat (already installed) + web frontend; per-interface daily/monthly graphs | Low | `vnstati` + simple PHP/HTML page |
| 33 | **Prometheus + Node Exporter** — scrape router metrics from homelab Grafana over Tailscale; CPU, memory, temp, interface stats | Low | `prometheus-node-exporter` |
| 34 | **Real-Time Traffic Inspector** — live per-second bandwidth by interface and connection; useful for "what's eating my bandwidth" debugging | Low-Med | `bmon` / `iftop` / `netdata` |
| 35 | **Tailscale Peer Status + ntfy Enrichment** — parse `tailscale status --json` every 5 min; ntfy alert when tunnel goes stale, peer drops, or handshake fails | Low | `jq` + `curl` (already have both) |

---

## Priority Picks (best ROI given current stack)

**Do these next — low effort, high travel value:**
- Feature 16 (Encrypted DNS) — biggest daily privacy gap still open
- Feature 28 (Avahi mDNS) — 5 min setup, unlocks home device discovery over Tailscale
- Feature 26 (Unattended upgrades) — passive; just turn it on
- Feature 32 (vnStat dashboard) — vnStat already running, just need a web frontend
- Feature 35 (Tailscale peer dashboard) — 50 lines of bash, uses ntfy.sh already deployed

**High-value medium effort:**
- Feature 18 (AdGuard Home) — single binary replaces dnsmasq DNS, adds per-client analytics; rivals $150 GL.iNet feature set
- Feature 31 (MAC clone for portals) — combined with captive-check.sh, fully automates hotel WiFi onboarding
- Feature 24 (Android tethering) — real second-carrier redundancy for pennies
- Feature 17 (VPN kill switch) — makes the VPN setup fail-safe

**Only if specific need:**
- Feature 20 (Tor proxy) — significant throughput hit, for high-risk travel only
- Feature 25 (UPS HAT) — requires hardware, but definitively solves SD corruption
- Feature 22 (Domain split tunnel) — complex to maintain; only if you need selective VPN routing

---

*Sources: GL.iNet v4.8 firmware docs, OpenWrt travelmate, itiligent/OpenWRT-Raspi-TravelRouter, bufferbloat.net, raspberrypi/bookworm-feedback, ArchWiki iPhone tethering, Cloudflare DoT guide*
