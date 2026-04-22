# Improvements & Roadmap

Items marked ✅ are already deployed. The rest are candidates for future implementation, ordered by impact.

---

## Already Deployed

| Feature | Files |
|---|---|
| ✅ Auto iPhone tether (udev) | `config/90-ipheth.rules`, `scripts/start-tether.sh`, `scripts/stop-tether.sh` |
| ✅ Uplink failover watchdog | `scripts/failover-watchdog.sh`, `systemd/failover-watchdog.*` |
| ✅ log2ram (SD card protection) | `/etc/log2ram.conf` |
| ✅ iptables-persistent save | `/etc/iptables/rules.v4` |
| ✅ IPv6 disable on uplinks | `config/99-disable-ipv6-uplink.conf` |
| ✅ TCP BBR + FQ | `config/tcp-bbr.conf` |
| ✅ TTL=65 mangling (Visible bypass) | `config/rc.local` |
| ✅ Tailscale + subnet routing | `config/99-tailscale.conf` |
| ✅ iPhone keepalive ping | `scripts/keepalive.sh` |

---

## High Priority

### 1. Migrate TTL Mangling from iptables to nftables
**What:** Replace `iptables -t mangle` TTL rules with nftables equivalent.

**Why:** On Raspberry Pi OS Bookworm, iptables' legacy compatibility shims have degraded on current kernels. nftables is the native, reliable path going forward. Also covers IPv6 hop-limit mangling in the same ruleset (iptables requires a separate `ip6tables` command that's easy to forget).

```bash
nft add table inet mangle
nft add chain inet mangle forward { type filter hook forward priority mangle \; }
nft add rule inet mangle forward oifname { "uap0", "wlan0", "eth0" } ip ttl set 65
nft add rule inet mangle forward oifname { "uap0", "wlan0", "eth0" } ip6 hop-limit set 65
```

Persist via `/etc/nftables.conf` and `systemctl enable nftables`. Disable `iptables-persistent` once migrated to avoid conflicts.

*Source: [Visible TTL bypass nftables fix 2025](https://black.jmyntrn.com/2025/10/05/openwrt-visible-network-ttl-bypass-fix/)*

---

### 2. WAN Connectivity Watchdog (systemd, with recovery steps)
**What:** Upgrade the keepalive ping cron to a proper systemd service with graduated recovery: reassociate → restart dhcpcd → reboot.

**Why:** The current cron ping detects loss but does nothing about it. This watchdog attempts soft recovery before hard recovery, and only reboots after 3 consecutive failures (avoiding reboot loops at captive portals).

```bash
# /usr/local/sbin/wan-watchdog.sh
FAIL=0
while true; do
    if ping -c 2 -W 3 1.1.1.1 > /dev/null 2>&1; then
        FAIL=0
    else
        FAIL=$((FAIL+1))
        logger "wan-watchdog: fail #$FAIL"
        if   [ $FAIL -eq 1 ]; then wpa_cli -i wlan0 reassociate
        elif [ $FAIL -eq 2 ]; then systemctl restart dhcpcd
        elif [ $FAIL -ge 3 ]; then reboot
        fi
    fi
    sleep 30
done
```

*Source: Community pattern from multiple Pi travel router repos*

---

### 3. Captive Portal Detection + Tailscale Auto-Pause
**What:** Detect captive portals (by probing `connectivitycheck.gstatic.com`) and automatically pause Tailscale so the portal page is reachable. Re-enable Tailscale after a timeout or manual trigger.

**Why:** This is the #1 UX pain point for travel routers. With Tailscale running, captive portal redirects silently fail because Tailscale intercepts DNS. Guests connecting to your AP also can't reach the portal.

```bash
# /usr/local/sbin/captive-check.sh
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    http://connectivitycheck.gstatic.com/generate_204)
if [ "$HTTP_CODE" != "204" ]; then
    logger "captive portal detected — pausing Tailscale for 5 min"
    tailscale down
    sleep 300
    tailscale up --advertise-routes=10.3.141.0/24 --accept-dns=false
fi
```

Add to WAN watchdog on each new upstream connection event.

*Source: [OpenWrt travel router captive portal thread](https://forum.openwrt.org/t/openwrt-travel-router-for-vpn-how-to-deal-with-captive-portal/134802)*

---

### 4. hostapd HT Capability Tuning
**What:** Add 802.11n HT flags that RaspAP doesn't expose in its UI, pushing real throughput from ~72 Mbps to ~120–150 Mbps.

**Why:** Without these flags the Zero 2 W AP defaults to 20 MHz channels. Add to `/etc/hostapd/hostapd.conf` (or RaspAP's override file):

```
ieee80211n=1
wmm_enabled=1
ht_capab=[HT40][SHORT-GI-20][SHORT-GI-40][DSSS_CCK-40]
dtim_period=1
beacon_int=100
```

`[HT40]` requires a clear adjacent channel — falls back to 20 MHz in dense RF environments (hotels/airports). Use `[HT40+]` or `[HT40-]` depending on your channel (below or above the center).

*Source: [RaspAP AP basics docs](https://docs.raspap.com/features-core/ap-basics/)*

---

### 5. DTIM Period = 1 for iOS Client Latency
**What:** Reduce `dtim_period` to 1 in hostapd so iOS devices wake to check for buffered frames at every beacon.

**Why:** iOS aggressively power-manages its Wi-Fi radio using DTIM. With `dtim_period=2` (RaspAP default), there's up to ~200ms extra latency on new connection bursts. Setting it to 1 halves that — the single highest-impact latency tweak for iPhone/iPad clients.

Add to `/etc/hostapd/hostapd.conf`:
```
dtim_period=1
```

---

## Medium Priority

### 6. CAKE qdisc for Bufferbloat Control
**What:** Replace `fq` with CAKE (Common Applications Kept Enhanced) on uplink interfaces. BBR handles sender-side congestion; CAKE handles the egress queue on the router.

**Why:** Eliminates bufferbloat — the latency spikes when someone is downloading while another device is browsing. Available in Bookworm kernel, no extra packages needed.

```bash
tc qdisc replace dev enx<tether-iface> root cake bandwidth 20mbit besteffort
tc qdisc replace dev wlan0 root cake bandwidth 50mbit besteffort
```

Tune `bandwidth` to ~90% of actual uplink speed. CAKE uses ~15% more CPU than `fq_codel` — profile on Pi Zero 2 W before committing.

*Source: [Bufferbloat.net CAKE wiki](https://www.bufferbloat.net/projects/codel/wiki/Cake/)*

---

### 7. iptables FORWARD Chain Hardening
**What:** Lock down cross-client traffic on the AP so connected devices can't reach each other or probe the Pi's admin interfaces.

**Why:** Default RaspAP FORWARD policy is ACCEPT — any device on your AP can reach any other device, and potentially the Pi's RaspAP web UI (port 80) and SSH. Important in hotels/airports where you don't control who else might be on the same upstream.

```bash
# Block AP clients from reaching each other
iptables -I FORWARD -i uap0 -o uap0 -j DROP
# Block AP clients from reaching RaspAP UI and SSH
iptables -I INPUT -i uap0 -p tcp --dport 80 -j DROP
iptables -I INPUT -i uap0 -p tcp --dport 22 -j DROP
# Allow established connections back
iptables -I FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT
```

After adding, save with `netfilter-persistent save`.

---

### 8. dnsmasq Cache & Upstream Tuning
**What:** Three specific dnsmasq options that significantly improve DNS performance for travel use.

Create `/etc/dnsmasq.d/travel-tweaks.conf`:
```
cache-size=2048
min-cache-ttl=300
neg-ttl=60
no-resolv
server=1.1.1.1
server=9.9.9.9
dns-forward-max=300
```

`min-cache-ttl=300` overrides absurdly short TTLs common in hotel/captive portal DNS. Disable when first joining a new upstream network to avoid stale redirects.

---

### 9. CPU Governor — Performance Mode
**What:** Pin the Zero 2 W's Cortex-A53 cores to max frequency instead of `ondemand` scaling.

**Why:** The `ondemand` governor has ~50ms reaction lag. During packet forwarding bursts the CPU is still ramping from 600 MHz, causing drops. For a dedicated router, always-on performance mode is correct.

```bash
echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
```

Add to `/etc/rc.local` before `exit 0`. Increases idle power draw ~0.5W — negligible on USB power.

---

## Low Priority / Advanced

### 10. MAC Address Randomization on wlan0
Randomize the MAC address when connecting to hotel/airport Wi-Fi to prevent cross-property tracking. Add to wpa_supplicant network block:
```
mac_addr=1
```

### 11. ntfy.sh Push Notifications
Send push notifications to your phone on router events (WAN up/down, captive portal, tether connected). Free hosted service with iOS/Android apps:
```bash
curl -s -d "iPhone tether connected" ntfy.sh/your-private-topic-id
```
Add one-liners to the watchdog and tether scripts. Use a self-hosted ntfy instance on your Tailnet for reliability when WAN is down.

### 12. Selective Tailscale Routing by MAC
Route specific client devices through Tailscale exit node while others go direct — useful when you want your laptop behind VPN but a streaming device to use local IP.
```bash
iptables -t mangle -A PREROUTING -m mac --mac-source AA:BB:CC:DD:EE:FF -j MARK --set-mark 0x1
ip rule add fwmark 0x1 table 100
ip route add default via <tailscale-peer-ip> dev tailscale0 table 100
```

### 13. DNS-over-HTTPS (dnscrypt-proxy)
Encrypt DNS queries so Visible/Verizon can't see your lookups or use DNS for hotspot detection:
```bash
sudo apt install -y dnscrypt-proxy
```
Point dnsmasq upstream at `127.0.0.1:5300`.

### 14. Read-Only Root Filesystem (overlayfs)
Prevent SD card corruption from sudden power loss (common when unplugging from laptop USB). Enable via `raspi-config` → Performance Options → Overlay File System. Note: makes the system harder to update — disable overlay before apt upgrades.

### 15. Static DHCP Leases for Known Devices
Assign fixed IPs to your devices for predictable addressing and easier Tailscale ACLs:
```
# /etc/dnsmasq.d/static-leases.conf
dhcp-host=AA:BB:CC:DD:EE:FF,10.3.141.10,macbook
dhcp-host=11:22:33:44:55:66,10.3.141.11,ipad
```

---

## Sources

- [itiligent/OpenWRT-Raspi-TravelRouter](https://github.com/itiligent/OpenWRT-Raspi-TravelRouter)
- [RaspAP AP-STA Mode Documentation](https://docs.raspap.com/features-experimental/ap-sta/)
- [Visible TTL bypass nftables fix 2025](https://black.jmyntrn.com/2025/10/05/openwrt-visible-network-ttl-bypass-fix/)
- [iPhone tethering — ArchWiki](https://wiki.archlinux.org/title/IPhone_tethering)
- [Raspberry Pi bookworm-feedback #220 — AP-STA drops](https://github.com/raspberrypi/bookworm-feedback/issues/220)
- [CAKE qdisc — Bufferbloat.net](https://www.bufferbloat.net/projects/codel/wiki/Cake/)
- [OpenWrt captive portal + VPN thread](https://forum.openwrt.org/t/openwrt-travel-router-for-vpn-how-to-deal-with-captive-portal/134802)
- [morrownr/USB-WiFi — hostapd performance](https://github.com/morrownr/USB-WiFi/discussions/420)
