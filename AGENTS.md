# Travel Router — Agent Context

Pi Zero 2 W travel router project. This file gives AI agents the context needed to continue work on this repo.

## Pi Access

```
Host:     100.105.78.127  (Tailscale)
User:     neek
Password: Y4nk33s^!2
Sudo:     echo 'Y4nk33s^!2' | sudo -S <cmd>
Fallback: ssh neek@192.168.7.1  (USB gadget, requires Pi plugged into laptop via USB-C)
```

## Hardware

- Raspberry Pi Zero 2 W (BCM2710A1, armv7l)
- Pi OS Lite Bookworm, kernel 6.12.75
- WiFi: BCM43438 (brcmfmac driver) — single radio, 2.4 GHz only
- Single micro-USB OTG port (shared between USB gadget mode and iPhone USB tethering)

## Network Layout

```
iPhone ──[USB ipheth / BT PAN]──▶ Pi Zero 2 W ──[uap0 AP]──▶ WiFi clients
                                       │
Hotel WiFi ──[wlan0 STA]──────────────▶│
                                       │
MacBook ──[USB-C gadget usb0]─────────▶│ 192.168.7.1/24 (admin)
                                       │
Tailscale ──[tailscale0]──────────────▶│ 100.105.78.127
```

| Interface | Role | Subnet | Metric |
|-----------|------|--------|--------|
| uap0 | AP for clients | 10.3.141.0/24 | — |
| wlan0 | STA uplink (hotel/open WiFi) | DHCP | 600 |
| enx* | iPhone USB tether | DHCP | 100 |
| bnep0 | iPhone Bluetooth PAN | DHCP | 300 |
| usb0 | USB gadget (laptop admin) | 192.168.7.1/24 | — |
| tailscale0 | Tailscale mesh VPN | 100.x.x.x | — |

## Key File Locations (on Pi)

```
/etc/default/travel-router      # NTFY_TOPIC, IPHONE_BT_MAC, PUSHGW_URL
/etc/hostapd/hostapd.conf       # AP config (SSID, channel, 802.11n)
/etc/dnsmasq.d/                 # DNS/DHCP configs
/etc/iptables/rules.v{4,6}      # Saved firewall + TTL rules
/etc/tor/torrc                  # Tor transparent proxy config
/usr/local/bin/                 # All router scripts
/etc/systemd/system/            # All watchdog timers and services
/etc/udev/rules.d/90-ipheth.rules
/etc/udev/rules.d/99-apple-autosuspend.rules
/var/log/wan-watchdog.log
/var/log/failover-watchdog.log
```

## What's Deployed and Working

- RaspAP (lighttpd + hostapd + dnsmasq), AP/STA concurrent mode on uap0
- iOS USB tethering: udev auto-detect (05ac vendor), start/stop-tether.sh
- Uplink failover watchdog: 30s timer, tether metric 100 / WiFi metric 600 / BT metric 300
- WAN watchdog + graduated recovery: 60s timer, reassociate → restart → reboot
- Captive portal detection + Tailscale pause (captive-check.sh)
- ntfy.sh push notifications via notify-router.sh
- USB Ethernet gadget (g_ether/dwc2): 192.168.7.1/24 — **REQUIRES REBOOT TO ACTIVATE**
- Open WiFi fallback (wpa_supplicant priority=1)
- Tailscale + subnet router (10.3.141.0/24), exit node capable
- TTL=65 iptables mangle + IPv6 hop-limit=65 (Visible carrier bypass)
- IPv6 disabled on uplinks (DPI fingerprint protection)
- DSCP strip on uplinks (iptables mangle, carrier bypass)
- IPv6 extension header drop (ip6tables mangle)
- TCP BBR + CAKE qdisc (bufferbloat control)
- CPU performance governor (systemd oneshot)
- hostapd 802.11n: HT40, WMM, DTIM=1
- dnsmasq tuning: cache-size=2048, min-cache-ttl=300, dns-forward-max=300
- DNS rebinding protection (stop-dns-rebind in dnsmasq)
- FORWARD chain client isolation + INPUT block on uap0 ports 80/22
- MAC randomization: NetworkManager conf.d + wlan-mac-random.service
- WiFi power save disabled on wlan0 (NetworkManager conf.d)
- brcmfmac roamoff=1 feature_disable=0x82000 (modprobe.d)
- log2ram: /var/log in RAM (SIZE=128M, JOURNALD_AWARE=true)
- iptables-persistent saves
- USB autosuspend disabled for Apple devices (udev, prevents ipheth drops)
- privoxy: transparent HTTP User-Agent normalization (port 8118, uap0 redirect)
- vnStat + Prometheus textfile exporter (5-min timer to /var/lib/prometheus/node-exporter/)
- usbmuxd hardening: Restart=on-failure, CPUQuota=20%
- Tor installed, torrc configured for transparent proxy (TransPort 9040, DNSPort 5353)

## Pending Tasks

These were interrupted when the Pi crashed (OOM from large nftables set load):

### 1. nftables Threat Intel Blocklist (#41) — PARTIALLY DONE
The script `update-blocklists.sh` is deployed at `/usr/local/bin/update-blocklists.sh` (RAM-safe Python version). The timer is enabled. **But the initial blocklist load has not succeeded yet.** Run it and verify:
```bash
sudo systemctl start update-blocklists.service
sudo journalctl -u update-blocklists.service -n 20 --no-pager
sudo nft list set inet blocklists firehol_l1 2>/dev/null | head -5
```
The script uses Python to build the nft file, avoiding the OOM that killed the Pi previously. If it still fails, reduce the blocklist to firehol_level2 (smaller) or chunk the load.

### 2. Tor Transparent Proxy iptables Rules (#42) — NOT DONE
Tor is installed and running. The torrc has TransPort 9040 and DNSPort 5353. But the iptables NAT rules that redirect 172.16.100.0/24 subnet traffic through Tor have **not been applied yet**. Apply and save:
```bash
TOR_SUBNET="172.16.100.0/24"
sudo iptables -t nat -A PREROUTING -s "$TOR_SUBNET" -p udp --dport 53 -j REDIRECT --to-ports 5353
sudo iptables -t nat -A PREROUTING -s "$TOR_SUBNET" -p tcp -d 10.0.0.0/8 -j RETURN
sudo iptables -t nat -A PREROUTING -s "$TOR_SUBNET" -p tcp -d 172.16.0.0/12 -j RETURN
sudo iptables -t nat -A PREROUTING -s "$TOR_SUBNET" -p tcp -d 192.168.0.0/16 -j RETURN
sudo iptables -t nat -A PREROUTING -s "$TOR_SUBNET" -p tcp --syn -j REDIRECT --to-ports 9040
sudo iptables-save > /etc/iptables/rules.v4
```
Also check if brcmfmac supports a second virtual AP for the TorAP SSID:
```bash
sudo iw dev wlan0 interface add uap1 type __ap 2>&1
```
If uap1 is supported: add second BSS to hostapd.conf, add dnsmasq-tor-ap.conf (in repo at config/dnsmasq-tor-ap.conf), add uap1 creation to rc.local.
If not: document the static-IP fallback (clients on uap0 set 172.16.100.x static IP).

### 3. Bluetooth Tethering (#43) — NOT DONE
Scripts are in the repo (`scripts/start-bt-tether.sh`, `scripts/stop-bt-tether.sh`) but not yet deployed to the Pi. Deploy and verify:
```bash
sudo apt-get install -y bluez bluez-tools python3-dbus
sudo cp scripts/start-bt-tether.sh /usr/local/bin/ && sudo chmod +x /usr/local/bin/start-bt-tether.sh
sudo cp scripts/stop-bt-tether.sh  /usr/local/bin/ && sudo chmod +x /usr/local/bin/stop-bt-tether.sh
sudo systemctl enable --now bluetooth
```
Add IPHONE_BT_MAC="" to /etc/default/travel-router if not present.
Patch /usr/local/bin/failover-watchdog.sh to check bnep0 (metric 300) between USB tether (100) and WiFi (600).

### 4. Captive Portal Auto-Login (#47) — NOT DONE
Add `attempt_portal_login()` function to `/usr/local/bin/captive-check.sh`. The function is documented in IMPROVEMENTS.md. Read the current captive-check.sh first, add the function, call it from the portal-detected branch, verify with `bash -n`.

### 5. Push Pi's live config back to repo
After completing the above, pull the live versions of these files from the Pi and commit them to keep the repo in sync:
- `/usr/local/bin/captive-check.sh` (after #47 edit)
- `/usr/local/bin/failover-watchdog.sh` (after #43 edit)
- `/etc/iptables/rules.v4` (after all iptables work)

## Repo Structure

```
install.sh          # Full installer for fresh Pi OS Bookworm
README.md           # Architecture, features, usage
IMPROVEMENTS.md     # Feature roadmap (deployed ✅ + roadmap items #1-50)
GL-MT3000.md        # GL-MT3000 synergy guide (8 deployment scenarios)
scripts/            # All /usr/local/bin/ scripts
config/             # All config file templates
systemd/            # All .service and .timer units
```

## Important Notes

- Pi Zero 2W has **one OTG port**: USB gadget mode (usb0→laptop) and iPhone USB tethering (enx*) are mutually exclusive. Use iPhone WiFi hotspot when using USB gadget.
- brcmfmac may only support **one virtual AP interface** (uap0). Test before building Tor SSID on uap1.
- The nftables blocklist load previously caused an OOM crash. The current `update-blocklists.sh` uses Python and should be safe, but monitor RAM during first run: `watch -n1 free -m`
- Tor on Pi Zero 2W is slow (~1-3 Mbps). It's functional but throughput-limited.
- RaspAP web UI: http://10.3.141.1 (admin / secret) — manages hostapd/dnsmasq via web
- iptables-nft is the backend on Bookworm — `nft list ruleset` and `iptables -L` both work
