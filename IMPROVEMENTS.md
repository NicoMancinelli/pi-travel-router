# Improvements & Roadmap

Items marked ✅ are deployed. The rest are future candidates, ordered by impact within each category.

---

## Deployed Features

| ✅ | Feature | Notes |
|---|---|---|
| ✅ | RaspAP (lighttpd, hostapd, dnsmasq) | AP/STA concurrent mode on uap0 |
| ✅ | iOS USB tether auto-detect (udev) | Plug in iPhone → DHCP fires automatically |
| ✅ | Uplink failover watchdog | 30s timer; USB tether metric 100, Bluetooth PAN metric 300, wlan0 metric 600 |
| ✅ | WAN watchdog with graduated recovery | 60s timer; reassociate → restart dhcpcd → reboot |
| ✅ | Captive portal detection + Tailscale pause | Probes generate_204; pauses/restores Tailscale automatically |
| ✅ | ntfy.sh push notification framework | Set `NTFY_TOPIC` in `/etc/default/travel-router` |
| ✅ | USB Ethernet Gadget (g_ether) | Laptop via USB-C → micro-USB; 192.168.7.1/24 **[needs reboot]** |
| ✅ | Open WiFi fallback | Available but disabled by default via `ENABLE_OPEN_WIFI_FALLBACK=0` |
| ✅ | Tailscale + subnet routing (10.3.141.0/24) | Remote access over mesh VPN |
| ✅ | IP forwarding (IPv4 + IPv6) | `/etc/sysctl.d/99-tailscale.conf` |
| ✅ | TTL=65 iptables mangling | uap0, wlan0, eth+, enx+ — bypasses Visible hotspot detection |
| ✅ | IPv6 hop-limit=65 (ip6tables) | Mirrors TTL mangling for IPv6 traffic |
| ✅ | IPv6 disabled on uplink interfaces | Closes DPI fingerprinting vector TTL alone doesn't cover |
| ✅ | DSCP strip | Clears carrier ToS fingerprinting on uplinks |
| ✅ | IPv6 extension header drop | Drops hop-by-hop extension headers on wlan0 when supported |
| ✅ | TCP BBR + FQ qdisc | Better cellular throughput; persists via modules-load.d |
| ✅ | CAKE qdisc for bufferbloat | wlan0 at 50mbit; tether at 15mbit via start-tether.sh |
| ✅ | CPU performance governor | Systemd oneshot; eliminates ramp-up latency spikes |
| ✅ | hostapd 802.11n (HT40, WMM, DTIM=1) | ~150 Mbps AP; DTIM=1 halves iOS client wake latency |
| ✅ | dnsmasq tuning | cache-size=2048, min-cache-ttl=300, dns-forward-max=300 |
| ✅ | DNS rebinding protection | `stop-dns-rebind` plus local/lan exceptions |
| ✅ | FORWARD chain client isolation | AP clients can't reach each other or Pi admin interfaces |
| ✅ | INPUT: block AP clients from port 80/22 | Manage only via Tailscale or USB gadget (192.168.7.x) |
| ✅ | MAC address randomization (wlan0) | NetworkManager + macchanger systemd service |
| ✅ | log2ram | `/var/log` in RAM; protects SD card (active after reboot) |
| ✅ | Idempotent firewall script | `/usr/local/bin/travel-router-firewall.sh --save` owns TTL, DSCP, isolation, optional proxy rules |
| ✅ | iptables-persistent save | Firewall rules saved in `/etc/iptables/rules.v{4,6}` |
| ✅ | iPhone keepalive (replaced by WAN watchdog) | Legacy script retained but not installed by default |
| ✅ | Static DHCP leases template | `/etc/dnsmasq.d/static-leases.conf` — fill in your MACs |
| ✅ | Bluetooth PAN tethering (#43) | `start-bt-tether.sh` + bnep0 as metric-300 uplink; set `IPHONE_BT_MAC` in `/etc/default/travel-router` |
| ✅ | Captive portal auto-login (#47) | `attempt_portal_login()` in captive-check.sh; per-SSID hooks in `/etc/travel-router/portals/<SSID>.sh` |
| ✅ | Captive portal MAC clone (#31) | `clone-mac.sh <MAC>` — clones laptop MAC to wlan0 before portal auth; `--restore` to reset |
| ✅ | Threat intel IP blocklist (#41) | `update-blocklists.sh` + daily timer; enable with `ENABLE_BLOCKLISTS=1` in `/etc/default/travel-router` |
| ✅ | Tor transparent proxy (#42) | Installed, disabled by default; enable with `ENABLE_TOR_TRANSPARENT=1`; uap1 (TorAP SSID) probed at install time |
| ✅ | Auto-update from GitHub | `update-router.sh` + weekly timer (Sun 03:00); checks releases, falls back to main SHA; run manually with `sudo update-router.sh` |
| ✅ | DNS-over-TLS (#16) | `stubby` → Cloudflare + Quad9; dnsmasq forwards via `server=127.0.0.1#5300`; enable with `ENABLE_DOT=1` |
| ✅ | VPN kill switch (#17) | `KILL_SWITCH` iptables chain in `travel-router-firewall.sh`; blocks AP traffic when Tailscale drops; enable with `ENABLE_VPN_KILLSWITCH=1` |
| ✅ | Unattended security updates (#26) | `unattended-upgrades` + auto-reboot at 03:30 + ntfy.sh notify; enable with `ENABLE_AUTO_UPDATES=1` |
| ✅ | Android USB tethering (#24) | RNDIS/CDC-ECM udev rules; `rndis0`/`usb0` as metric-200 uplink; plug in Android with USB tethering on |
| ✅ | Avahi mDNS reflector (#28) | bridges mDNS between uap0 and tailscale0; AirPrint/AirPlay/NAS discovery; enable with `ENABLE_AVAHI_REFLECTOR=1` |
| ✅ | Tailscale peer watchdog (#35) | 5-min health check; ntfy alert on daemon down, stale handshake, or peer loss |
| ✅ | AdGuard Home (#18) | DNS ad-blocker + per-client analytics + DoT upstreams; web UI at `:3000`; enable with `ENABLE_ADGUARD=1` |
| ✅ | Scheduled AP disable (#29) | `ap-disable.timer` / `ap-enable.timer`; disable at 02:00, re-enable at 07:00; enable with `ENABLE_AP_SCHEDULE=1` |
| ✅ | Per-client bandwidth fairness (#21) | CAKE `per-host` on uap0; prevents one device starving others; set `AP_CLIENT_BANDWIDTH` for hard cap; `ENABLE_CLIENT_QOS=1` |
| ✅ | Per-device Tailscale routing (#44) | fwmark 0x64 + routing table 100; specified MACs routed through Tailscale, others direct; set `VPN_DEVICE_MACS` + `ENABLE_PER_DEVICE_VPN=1` |
| ✅ | Hardware watchdog | BCM2835 dtoverlay + systemd `RuntimeWatchdogSec=15`; auto-reboot on kernel lockup (active after reboot) |
| ✅ | Log rotation | `logrotate.d/travel-router` — daily rotation, 7-day retention, compressed |
| ✅ | Daily digest notification | 08:00 ntfy push: uptime, active uplink, Tailscale state, AP clients, failed units; fires only when `NTFY_TOPIC` set |

Optional Privoxy HTTP User-Agent rewriting, Tor transparent proxying, and nftables blocklists are installed as templates/scripts but disabled by default until tested on the target Pi.

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

## Roadmap — 18 Additional Features (From Cross-Project Research)

Research sources: Juraj Bednar bypass-anti-tethering, xiv3r bypass-anti-tethering, MobileHop TCP fingerprint docs, PORTAL onion router, banIP/OpenWrt, OpenMPTCProuter, Headscale, Firewalla, captive-login bash utility, pfSense DNS rebind docs, PiSugar 3 hardware docs, Sagan/ntopng on Pi research.

### Carrier Bypass (Beyond TTL)

| # | Feature | Effort | Package/Tool |
|---|---|---|---|
| 36 | **DNS Rebinding Protection** — dnsmasq `stop-dns-rebind` + `rebind-localhost-ok`; blocks hotel/captive-portal DNS poisoning that redirects your queries to private RFC1918 IPs | 10 min | dnsmasq config |
| 37 | **DSCP Remarking Strip** — strip DSCP/ToS bits on upstream traffic; some carriers use DSCP to fingerprint hotspot vs. native traffic. One iptables `MARK` + `TOS` rule on FORWARD chain | 5 min | iptables |
| 38 | **IPv6 Extension Header Normalization** — strip/normalize IPv6 extension headers (hop-by-hop options) on uplink; used by some DPI systems to fingerprint tethered traffic differently from native | 15 min | ip6tables |
| 39 | **HTTP User-Agent Normalization (tinyproxy)** — transparent HTTP proxy rewrites User-Agent to match a common Android browser; closes UA-based hotspot fingerprinting | 2 hr | `tinyproxy` |
| 40 | **TCP/IP Fingerprint OS Spoofing (p0f/NFQUEUE)** — rewrite TCP window size + options at the IP layer to look like an Android device instead of a router; defeats advanced Visible/carrier fingerprinting that TTL alone doesn't cover | 4–8 hr | `nfqueue`-based shim or `nftables` osf match |

### Security

| # | Feature | Effort | Package/Tool |
|---|---|---|---|
| 41 | **Threat Intel IP Blocking (banIP/nftables sets)** — fetch Firehol Level 1 blocklist + known Tor exit nodes + malware C2 IPs into nftables `ip sets`; auto-refresh via systemd timer | 2–3 hr | `nftables` sets + `curl` (already installed) |
| 42 | **Transparent Tor SSID** — second SSID (`TorAP`) that routes all TCP through Tor's TransPort; clients connect to it for anonymized browsing without per-device config; separate subnet (172.16.x.x) | 2–3 hr | `tor` + iptables NAT |

### Multi-WAN & Policy Routing

| # | Feature | Effort | Package/Tool |
|---|---|---|---|
| 43 | **Bluetooth Tethering as Tertiary WAN** — pair iPhone via Bluetooth, use `bnep0` interface as a low-bandwidth 3rd uplink; useful when both WiFi and USB are unavailable | 2–3 hr | `bluez` + `NetworkManager` |
| 44 | **Per-Device fwmark Split Tunnel** — assign a specific AP client MAC a fwmark, route that device's traffic through Tailscale exit node while others go direct; 20 lines of nftables + `ip rule` | 3–4 hr | `nftables` fwmark + `ip rule` |
| 45 | **Domain-Based Split Tunnel (dnsmasq ipset)** — specific domains (banking, corporate) resolve into an `ipset`; fwmark routes matching traffic through Tailscale tunnel while all other traffic goes direct | 4–6 hr | `ipset` + dnsmasq `ipset=` directive |

### VPN & Remote Access

| # | Feature | Effort | Package/Tool |
|---|---|---|---|
| 46 | **Headscale (Self-Hosted Tailscale Control Server)** — replace the Tailscale cloud coordination server with self-hosted Headscale on your VPS; full control of your Tailnet topology, no Tailscale account dependency, zero telemetry | 3–4 hr + VPS | `headscale` binary |

### Captive Portal Automation

| # | Feature | Effort | Package/Tool |
|---|---|---|---|
| 47 | **Captive Portal Auto-Login (curl scripts)** — extend captive-check.sh with per-SSID login scripts; for known hotel chains, auto-submit the portal form via curl; pattern from authq/captive-login | 2–3 hr | `curl` (already installed) |

### Observability

| # | Feature | Effort | Package/Tool |
|---|---|---|---|
| 48 | **vnStat + Prometheus Push** — vnStat's per-interface monthly stats pushed to homelab Prometheus via curl push gateway over Tailscale; adds cellular data usage tracking alongside existing CPU/memory metrics | 1–2 hr | `vnstat-json` + `curl` |
| 49 | **Lightweight IDS (Sagan log correlator)** — correlate dnsmasq, iptables, and auth logs in real time; alert via ntfy.sh on port-scan patterns or DNS exfil signatures; Sagan runs on Pi-class hardware unlike Suricata | 4–6 hr | `sagan` + existing ntfy.sh |

### Reliability & Hardware

| # | Feature | Effort | Package/Tool |
|---|---|---|---|
| 50 | **PiSugar 3 UPS + Hardware Watchdog + RTC** — I2C battery board attaches to Pi GPIO; monitors charge level, triggers safe shutdown before depletion, adds hardware watchdog (GPIO pin), and provides RTC for accurate time without NTP; definitively solves SD corruption and time drift | 1 hr (sw) + hardware | PiSugar 3 board (~$25) |

---

## Priority Picks (best ROI given current stack)

**Do these next — low effort, high travel value:**
- Feature 16 (Encrypted DNS / DoT) — biggest daily privacy gap still open
- Feature 28 (Avahi mDNS) — 5 min setup, unlocks home device discovery over Tailscale
- Feature 26 (Unattended upgrades) — passive; just turn it on
- Feature 35 (Tailscale peer dashboard) — 50 lines of bash, uses ntfy.sh already deployed

**High-value medium effort:**
- Feature 18 (AdGuard Home) — single binary replaces dnsmasq DNS, adds per-client analytics; rivals $150 GL.iNet feature set
- Feature 46 (Headscale) — eliminates Tailscale cloud dependency
- Feature 24 (Android tethering) — real second-carrier redundancy
- Feature 17 (VPN kill switch) — makes the VPN setup fail-safe

**Only if specific need:**
- Feature 40 (TCP fingerprint spoofing) — significant implementation effort, marginal carrier bypass benefit unless TTL alone fails
- Feature 42 (Tor SSID) — significant throughput hit, for high-risk travel only
- Feature 49 (Sagan IDS) — worthwhile if you want edge threat detection; heavier to maintain
- Feature 25/50 (UPS HAT/PiSugar 3) — requires hardware, but definitively solves SD corruption
- Feature 45 (Domain split tunnel) — complex to maintain; only if you need selective VPN routing by domain

---

*Sources: GL.iNet v4.8 firmware docs, OpenWrt travelmate, itiligent/OpenWRT-Raspi-TravelRouter, bufferbloat.net, raspberrypi/bookworm-feedback, ArchWiki iPhone tethering, Cloudflare DoT guide, Juraj Bednar bypass-anti-tethering, xiv3r bypass-anti-tethering, PORTAL onion router, banIP OpenWrt, OpenMPTCProuter, Headscale docs, Firewalla deep insight, authq/captive-login, pfSense DNS rebind docs, PiSugar 3 CNX Software review, Sagan IDS HookProbe guide*
