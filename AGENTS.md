# Travel Router — Agent Context

Pi Zero 2 W travel router project. This file gives AI agents the context needed to continue work on this repo.

## Pi Access

```
Host:     <TAILSCALE_IP>  (Tailscale)
User:     <PI_USER>
Password: <never commit live credentials>
Sudo:     use interactive sudo or a local-only secret note
Fallback: ssh <PI_USER>@192.168.7.1  (USB gadget, requires Pi plugged into laptop via USB-C)
```

Keep live hostnames, passwords, auth keys, and ntfy topics in `AGENTS.local.md`
or a local password manager. `AGENTS.local.md` is intentionally ignored by git.
If a live credential was committed, rotate it before continuing.

## Hardware

- Raspberry Pi Zero 2 W (BCM2710A1, aarch64 (arm64))
- Pi OS Lite Bookworm (check current kernel with `uname -r` on the device)
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
Tailscale ──[tailscale0]──────────────▶│ <TAILSCALE_IP>
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
/etc/default/travel-router      # NTFY_TOPIC, IPHONE_BT_MAC, PUSHGW_URL, feature flags
/etc/hostapd/hostapd.conf       # AP config (SSID, channel, 802.11n)
/etc/dnsmasq.d/                 # DNS/DHCP configs
/etc/iptables/rules.v{4,6}      # Saved firewall + TTL rules
/etc/nftables.conf.d/travel-router.nft  # nftables TTL/DSCP ruleset
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
- USB Ethernet gadget (g_ncm/dwc2): 192.168.7.1/24 — pre-enabled in the image; no post-install reboot needed for USB gadget reachability
- Open WiFi fallback is available but disabled by default (`ENABLE_OPEN_WIFI_FALLBACK=0`)
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
- privoxy: HTTP User-Agent normalization config installed; redirect disabled by default
- vnStat + Prometheus textfile exporter (5-min timer to /var/lib/prometheus/node-exporter/)
- usbmuxd hardening: Restart=on-failure, CPUQuota=20%
- Tor installed; transparent proxy config/rules disabled by default

## Pending Tasks

To update a running Pi, run `sudo update-router.sh` after pushing changes to main.

## Completed

- ✅ #43 Bluetooth tethering — scripts in repo, bluez installed by install.sh, failover-watchdog handles bnep0
- ✅ #47 Captive portal auto-login — `attempt_portal_login()` added to captive-check.sh; per-SSID hooks go in /etc/travel-router/portals/<SSID>.sh
- ✅ Auto-update — `update-router.sh` + weekly timer; checks GitHub releases, falls back to main branch SHA; updates scripts + systemd units, never touches user config files
- ✅ install.sh fully reproducible — interactive prompts for all optional features, uap1 probe + Tor AP setup, blocklist first-load trigger

## Repo Structure

```
install.sh          # Full installer for fresh Pi OS Bookworm
README.md           # Architecture, features, usage
IMPROVEMENTS.md     # Feature roadmap (deployed ✅ + roadmap items #1-50)
GL-MT3000.md        # GL-MT3000 synergy guide (8 deployment scenarios)
scripts/            # All /usr/local/bin/ scripts (run on the Pi)
config/             # All config file templates
systemd/            # All .service and .timer units
tools/              # Helper scripts for external infrastructure (not the Pi itself)
                    #   setup-headscale.sh — run on a public VPS to install Headscale
```

## TUI Coverage Rule

`scripts/travel-tui.sh` is the primary management interface. Every new feature **must** be reflected there:

| What you add | Where in TUI |
|---|---|
| New `ENABLE_*` flag in `config/travel-router-defaults` | Add to `flag_list` array in `show_features()` + add a `case` handler for service restart |
| New config variable in `config/travel-router-defaults` | Add to `show_settings()` with a display line and a `case` handler calling `_cfg_edit` |
| New user-triggerable script in `scripts/` | Add to `show_system()` (maintenance scripts) or `show_network()` (connectivity scripts) |
| New systemd service worth monitoring | Add to `show_services()` service list |

After adding to the TUI, run `bash -n scripts/travel-tui.sh` to verify syntax.

## Important Notes

- Pi Zero 2W has **one OTG port**: USB gadget mode (usb0→laptop) and iPhone USB tethering (enx*) are mutually exclusive. Use iPhone WiFi hotspot when using USB gadget.
- brcmfmac may only support **one virtual AP interface** (uap0). Test before building Tor SSID on uap1.
- The nftables blocklist load previously caused an OOM crash. The current `update-blocklists.sh` caps generated entries; monitor RAM during first enabled run.
- Tor on Pi Zero 2W is slow (~1-3 Mbps). Keep it disabled unless that tradeoff is acceptable.
- RaspAP web UI: http://10.3.141.1 (admin / secret) — manages hostapd/dnsmasq via web
- iptables-nft is the backend on Bookworm — `nft list ruleset` and `iptables -L` both work
