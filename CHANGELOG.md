# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.57.0] - 2026-05-25

### Added
- `GET /api/network/interfaces` — all network interfaces with IP/MAC addresses, operstate, MTU, and Rx/Tx traffic totals via `ip -j addr` + `ip -j -s link`; falls back to text parsing when JSON unsupported
- `GET /api/system/services` — status of key travel-router systemd services (`wg-quick@wg0`, `hostapd`, `dnsmasq`, `fail2ban`, etc.) via `systemctl is-active/is-enabled`
- Dashboard cards: Network Interfaces (🖧) with UP/DOWN badge + address list, System Services (⚙️) with running/enabled state per service

### Fixed
- Removed duplicate `POST /api/network/interfaces` route (proc/net/dev stub) that caused Flask startup failure

## [2.56.0] - 2026-05-25

### Added
- `GET /api/system/thermal-history` — thermal zone readings from `/sys/class/thermal/thermal_zone*/temp` with zone types; `vcgencmd measure_temp` GPU temp when available; dashboard card color-codes temps (green <60°C, yellow 60–80°C, red ≥80°C)
- `GET /api/network/neighbors` — ARP/NDP neighbor table via `ip neigh show` / `ip -6 neigh show`, filtered to entries with MAC addresses, deduped by IP; dashboard card highlights REACHABLE (green) and STALE (yellow) states
- `GET /api/system/process-tree` — top 20 processes by CPU from `ps -eo pid,ppid,user,%cpu,%mem,comm --sort=-%cpu`; dashboard card color-codes high CPU usage
- Dashboard cards: Thermal Zones (🌡️), Network Neighbors (🔗), Top Processes (🌳)

## [2.55.0] - 2026-05-25

### Added
- `GET /api/network/iptables` — iptables/ip6tables chain rules parsed into INPUT/FORWARD/OUTPUT dicts; dashboard card shows collapsible chain details with scrollable `<pre>` blocks
- `GET /api/system/package-updates` — apt upgradable packages with current/available versions and architecture; dashboard card shows green checkmark when up-to-date or a table of pending updates
- `GET /api/network/bandwidth` — live TX/RX rates via 1-second `/proc/net/dev` delta sampling per interface; dashboard card shows human-readable rates (B/s, KB/s, MB/s) and cumulative totals
- Dashboard cards: Firewall Rules (🔥), Package Updates (📦), Live Bandwidth (📈)

## [2.54.0] - 2026-05-25

### Added
- `GET /api/system/log-summary` — journal error/warning counts from `journalctl -n 100`, grouped by service with recent message previews; dashboard card shows severity badges
- `GET /api/network/active-connections` — established TCP/UDP connections via `ss -tnup state established`, showing local/remote addresses, ports, PIDs, and process names
- `GET /api/network/wifi-clients` — connected Wi-Fi station list from `iw dev station dump`, showing MAC, signal, tx/rx rates, and connected time
- Dashboard cards: Log Summary (📋), Active Connections (🔌), Wi-Fi Clients (📡)

## [2.53.0] - 2026-05-25

### Added
- `GET /api/network/dns-config` — DNS resolver config from `/etc/resolv.conf` (nameservers, search, options), `resolvectl status` output, and AdGuard Home active status
- `GET /api/system/usb-devices` — USB device listing via `lsusb` with tree view from `lsusb -t`; dashboard card shows bus, device ID, and description
- `GET /api/network/ip-rules` — policy routing rules from `ip rule show`, parsed into priority/rule/table structure; useful for debugging multi-uplink failover
- Dashboard cards: DNS Config (🌐), USB Devices (🔌), IP Rules (📋)

## [2.52.0] - 2026-05-25

### Added
- `GET /api/system/uptime-history` — reboot/shutdown history from `last reboot/shutdown` (up to 10 entries each) with current uptime from `/proc/uptime`; dashboard card shows boot/down badges
- `GET /api/network/socket-summary` — socket statistics via `ss -s` (total, TCP states, UDP, RAW) with `/proc/net/sockstat` fallback
- `GET /api/system/block-devices` — block device listing via `lsblk --json` with key-value fallback; dashboard table shows name, size, type, filesystem, mount point, model with child partition indentation
- Dashboard cards: Uptime History (🔄), Socket Summary (🔗), Block Devices (💾)

## [2.51.0] - 2026-05-25

### Added
- `GET /api/network/open-ports` — listening TCP/UDP ports via `ss -tlunp` (netstat fallback), sorted by port number with process names and PIDs
- `GET /api/system/hardware` — Raspberry Pi hardware info: board model, hardware ID, revision, serial, RAM total, SD card size, and `vcgencmd get_throttled` state
- `GET /api/system/vmstat` — VM statistics from `/proc/vmstat` (paging, swapping, faults, OOM kills, dirty pages) plus `/proc/sys/vm` tunables (swappiness, dirty ratios, overcommit)
- Dashboard cards: Open Ports (🔌), Hardware Info (🖥), VM Statistics (📊)

## [2.50.0] - 2026-05-25

### Added
- `GET /api/system/failed-services` — systemd units in failed state with 5-line journalctl tail per unit; shows green checkmark when clean
- `GET /api/system/ntp-peers` — NTP peer status via `chronyc sources/tracking` (ntpq fallback) with stratum, offset, and sync state
- `GET /api/network/ip-geo` — public IP and geolocation from ip-api.com (country, region, city, ISP, org, ASN); useful to verify VPN exit node
- Dashboard cards: Failed Services (💥), NTP Peers (🕐), Public IP & Location (🌍)

## [2.49.0] - 2026-05-25

### Added
- `GET /api/system/cpu-governors` — CPU frequency scaling governor and current/min/max frequencies per core via `/sys/devices/system/cpu/*/cpufreq`
- `GET /api/network/mdns` — mDNS/Avahi service discovery via `avahi-browse` (dns-sd fallback), returns deduplicated service list with name, type, hostname, address, port
- `GET /api/system/timers` — systemd timers via `systemctl list-timers --all`, with next/last trigger times and activated unit
- Dashboard cards: CPU Governors (⚙️), mDNS Services (📡), System Timers (⏱)

## [2.48.0] - 2026-05-25

### Added
- `GET /api/system/top-processes` — top 15 processes by CPU and by memory (ps aux), with PID, user, percentages, and command
- `GET /api/network/wifi-info` — per-interface WiFi details via `iw dev/link/station dump`: SSID, signal, band, TX/RX bitrate, connected client stations
- `GET /api/system/entropy` — kernel entropy pool stats (`entropy_avail`, `pool_size`, `read_wakeup_threshold`), hardware RNG devices, and RNG daemon detection
- Dashboard cards: Top Processes (🔝), WiFi Details (📶), Entropy / RNG (🎲)

## [2.47.0] - 2026-05-25

### Added
- `GET /api/network/ping` — pings gateway, Cloudflare DNS, Google DNS, and Tailscale relay; returns RTT min/avg/max and packet loss per host
- `GET /api/system/cron-jobs` — enumerates scheduled tasks from `/etc/crontab`, `/etc/cron.d/*`, and root's crontab
- `GET /api/system/mounts` — lists non-virtual mount points with disk space (df -B1) and inode usage (df -i)
- Dashboard cards: Network Ping (🏓), Cron Jobs (⏰), Mount Points (💾)

## [2.46.0] - 2026-05-25

### Added

- **CPU utilisation card** (`web/app.py`, `web/static/index.html`): `GET /api/system/cpu-stats` reads `/proc/stat` for overall + per-core user/nice/system/idle/iowait/irq/softirq percentages. Dashboard card with overall utilisation bar (green/orange/red), breakdown row (User/System/IOWait/IRQ), compact per-core bars when multi-core. Auto-refreshes on poll.
- **Key sysctl card** (`web/app.py`, `web/static/index.html`): `GET /api/system/sysctl` reads 10 key `/proc/sys/` paths (ip_forward, ipv6 forwarding, syncookies, rp_filter, rmem_max, wmem_max, swappiness, dirty_ratio, hostname, randomize_va_space). Forwarding flags show green ✓/red ✗ badges, swappiness colour-coded, buffer sizes human-readable. On-demand Refresh + auto-polls.
- **Memory breakdown card** (`web/app.py`, `web/static/index.html`): `GET /api/system/memory-breakdown` reads `/proc/meminfo` for 20 fields normalised to bytes. Dashboard card with colour-coded usage bar (green <60%, orange 60-80%, red ≥80%), labelled rows (Used/Available/Buffers/Cached/Swap Used). Auto-refreshes on poll.

## [2.45.0] - 2026-05-25

### Added

- **Disk I/O stats card** (`web/app.py`, `web/static/index.html`): `GET /api/system/disk-io` reads `/proc/diskstats`, skips loop/ram devices, converts sectors to bytes (×512). Returns per-device reads_completed/read_bytes/writes_completed/written_bytes/time_reading_ms/time_writing_ms. Dashboard card with Device/Reads/Read Bytes/Writes/Written Bytes table, human-readable byte sizes. Auto-refreshes on poll.
- **Logged-in users card** (`web/app.py`, `web/static/index.html`): `GET /api/system/logged-in-users` parses `who -u` output extracting username/tty/login_time/idle/pid/from fields. Dashboard card with User/TTY/Login Time/Idle/From table, green dot for active sessions (idle="."), "Local" for console sessions. Auto-refreshes on poll.
- **Kernel modules card** (`web/app.py`, `web/static/index.html`): `GET /api/system/kernel-modules` reads `/proc/modules`, parses name/size/used/depends, returns top 50 by size with total count. Dashboard card with Module/Size/Used/Depends table, KB/MB size formatting, depends as comma-separated tags. On-demand refresh.

## [2.44.0] - 2026-05-25

### Added

- **CPU temperature card** (`web/app.py`, `web/static/index.html`): `GET /api/system/cpu-temp` globs `/sys/class/thermal/thermal_zone*/temp` and `type`, skips acpi zones reading 0. Returns zones array with zone name + temp in °C and `max_temp_c`. Dashboard card with large max-temp display, per-zone colour-coded badges (blue <50°C, green 50-65°C, orange 65-80°C, red ≥80°C). Auto-refreshes on poll.
- **USB devices card** (`web/app.py`, `web/static/index.html`): `GET /api/system/usb-devices` parses `lsusb` output extracting bus/device/vendor_id/product_id/description with best-effort sysfs enrichment via `/sys/bus/usb/devices/`. Dashboard card with Bus/Dev/ID/Description table, count summary header, network adapter badge for Ethernet/WLAN/Wireless devices. On-demand refresh.
- **Firewall rules card** (`web/app.py`, `web/static/index.html`): `GET /api/network/firewall-stats` runs `iptables -L -n -v --line-numbers` for IPv4 and `ip6tables` for IPv6. Parses chain headers for policy/packets/bytes, counts numbered rule lines, converts K/M/G byte suffixes. Dashboard card with Chain/Policy/Packets/Bytes/Rules table, ACCEPT=green/DROP|REJECT=red policy badges, human-readable byte sizes, IPv6 section hidden when empty. Auto-refreshes on poll.

## [2.43.0] - 2026-05-25

### Added

- **System load average card** (`web/app.py`, `web/static/index.html`): `GET /api/system/loadavg` reads `/proc/loadavg` (1m/5m/15m averages, running/total processes, last PID) and `/proc/cpuinfo` for CPU count. Dashboard card with load bars colour-coded relative to CPU count (green <50%, orange 50-80%, red ≥80%), process summary row. Auto-refreshes on poll.
- **OS info card** (`web/app.py`, `web/static/index.html`): `GET /api/system/osinfo` reads `/proc/version`, `/etc/os-release`, `uname -m`, `uptime -p`, and `/proc/sys/kernel/hostname`. Dashboard card showing hostname header with Distro/Kernel/Arch/Uptime table rows. Auto-refreshes on poll.
- **Socket statistics card** (`web/app.py`, `web/static/index.html`): `GET /api/network/sockstat` reads `/proc/net/sockstat` and `/proc/net/sockstat6`, parses per-protocol used/orphaned/tw/alloc/mem fields. Dashboard card with Protocol/In-Use/Orphaned/Time-Wait/Alloc table, orange warnings when tw>100 or orphan>0. Auto-refreshes on poll.

## [2.42.0] - 2026-05-25

### Added

- **Kernel log (dmesg) card** (`web/app.py`, `web/static/index.html`): `GET /api/system/dmesg` runs `dmesg --time-format iso -l warn,err,crit,alert,emerg` with fallback to `dmesg -T | tail -N`. Parses ISO and bracketed timestamps via regex, classifies messages as err/warn/info. `?lines=N` param (default 30, max 100). Dashboard card with per-message level badges (red=err/crit, orange=warn), dimmed timestamps, green "No kernel warnings" when clean. Auto-refreshes on poll.
- **Disk partitions card** (`web/app.py`, `web/static/index.html`): `GET /api/system/disk-partitions` runs `df -P -k`, cross-references `/proc/mounts` for filesystem type, skips tmpfs/devtmpfs/overlay/squashfs, merges `lsblk -J` device model info. Dashboard card with device/mountpoint/fstype/size table, inline usage bars (green <60%, orange 60-80%, red ≥80%), optional model subtitle. Auto-refreshes on poll.
- **Top processes by memory card** (`web/app.py`, `web/static/index.html`): `GET /api/system/proc-mem` reads `/proc/<pid>/status` for all live PIDs, extracts Name/VmRSS/VmSize/State with race-safe skip on OSError. Returns top 20 by RSS (configurable via `?limit=N`, max 50) with total RSS across all processes. Dashboard card with PID/Name/RSS/VSZ/State table, total RSS summary, refresh button. Auto-refreshes on poll.

## [2.41.0] - 2026-05-25

### Added

- **IRQ interrupts card** (`web/app.py`, `web/static/index.html`): `GET /api/system/interrupts` parses `/proc/interrupts`, sums counts across all CPUs, returns top 15 IRQs by total sorted descending. Dashboard card with IRQ/total/type/description table, count badge, on-demand Refresh button.
- **Battery / UPS status card** (`web/app.py`, `web/static/index.html`): `GET /api/system/battery` reads `/sys/class/power_supply/` sysfs entries, skips Mains/USB adapters, returns capacity, status, voltage, current, manufacturer, model, technology per battery. Dashboard card with per-battery capacity bar (green/orange/red), status badge (Charging/Discharging/Full), metadata row. Auto-refreshes on poll.
- **TCP connections card** (`web/app.py`, `web/static/index.html`): `GET /api/network/tcp` parses `/proc/net/tcp` and `/proc/net/tcp6`, converts little-endian hex addresses to dotted/colon notation, maps state hex to state names, skips loopback-only connections, returns top 50 sorted LISTEN-first then ESTABLISHED. Dashboard card with local/remote/state table, established+listening summary, state colour-coding. Auto-refreshes on poll.

## [2.40.0] - 2026-05-25

### Added

- **WiFi network scan card** (`web/app.py`, `web/static/index.html`): `GET /api/network/wifi-scan` tries `iw dev wlan0 scan` then falls back to `iwlist wlan0 scan`, parses SSID/BSSID/channel/signal/encryption, returns up to 20 networks sorted by signal strength. Dashboard card with signal-bar indicators (████/███░/██░░/█░░░), security label, on-demand Refresh button.
- **Interface counters card** (`web/app.py`, `web/static/index.html`): `GET /api/network/netdev` parses `/proc/net/dev`, returns RX/TX bytes/packets/errors per interface (skips `lo`, sorted alphabetically). Dashboard card with human-readable byte counts, error-row highlighting in orange. Auto-refreshes on poll.
- **Swap / zRAM usage card** (`web/app.py`, `web/static/index.html`): `GET /api/system/swap` reads `/proc/swaps` for per-device breakdown and cross-checks totals via `/proc/meminfo`. Returns total/used/free/pct + device list. Dashboard card with usage bar, zRAM badge, per-device table, graceful "no swap" state. Auto-refreshes on poll.

## [2.39.0] - 2026-05-25

### Added

- **ARP neighbor table card** (`web/app.py`, `web/static/index.html`): `GET /api/network/arp` parses `/proc/net/arp`, skips null MACs, maps flags to REACHABLE/STALE/INCOMPLETE/UNKNOWN states. Dashboard card with IP/MAC/Interface/State table and state colour-coding (green/orange/red/grey). Auto-refreshes on poll.
- **IP routing table card** (`web/app.py`, `web/static/index.html`): `GET /api/network/routes` runs `ip route show`, token-parses dest/via/dev/metric/proto/scope/src per route. Dashboard card with compact table, default route bolded, count badge. Auto-refreshes on poll.
- **Journal errors card** (`web/app.py`, `web/static/index.html`): `GET /api/system/journal-errors` runs `journalctl -p warning -n 30 --output=short-iso`, validates optional `?priority=` param against allowlist. Dashboard card with per-entry priority badges (red=err+, orange=warning), count badge, green "No errors" when clean. Auto-refreshes on poll.

## [2.38.0] - 2026-05-25

### Added

- **NTP / time sync status card** (`web/app.py`, `web/static/index.html`): `GET /api/system/ntp` runs `timedatectl show` and falls back to `chronyc tracking` / `ntpq -p` to return synced flag, timezone, NTP service, upstream server, stratum, and offset in ms. Dashboard card shows sync state with colour-coded badge, stratum, offset, and timezone. Auto-refreshes on poll.
- **Open file descriptors card** (`web/app.py`, `web/static/index.html`): `GET /api/system/openfiles` reads `/proc/sys/fs/file-nr` for kernel FD totals and scans `/proc/*/fd` symlinks to build a top-10 process list by open-FD count. Returns allocated/free/max/pct_used and per-process breakdown. Dashboard card with usage bar and process table. Auto-refreshes on poll.
- **CPU frequency card** (`web/app.py`, `web/static/index.html`): `GET /api/system/cpufreq` reads `/sys/devices/system/cpu/cpuN/cpufreq/` sysfs entries per core, returns cur_khz/min_khz/max_khz/governor/driver/available_governors. Dashboard card shows current/min/max MHz per core and active governor. Auto-refreshes on poll.

## [2.37.0] - 2026-05-25

### Added

- **Memory details card** (`web/app.py`, `web/static/index.html`): `GET /api/system/meminfo` parses `/proc/meminfo` and returns total/free/available/buffers/cached/swap stats in kB. Dashboard card shows RAM and swap usage bars with percentage and human-readable labels. Auto-refreshes on poll.
- **Firewall rules summary** (`web/app.py`, `web/static/index.html`): `GET /api/network/firewall` queries iptables filter/nat/mangle tables and returns chain names, policies, and rule counts per table. Dashboard card shows per-table chain breakdown with policy colour-coding (DROP=red, ACCEPT=green). On-demand refresh.
- **Disk I/O stats card** (`web/app.py`, `web/static/index.html`): `GET /api/system/diskio` parses `/proc/diskstats`, filters out loop/ram devices and partitions, returns reads/writes completed, bytes read/written, and I/O time in ms per whole-disk device. Dashboard card with compact table and human-readable byte counts. Auto-refreshes on poll.

## [2.36.0] - 2026-05-25

### Added

- **Kernel modules viewer** (`web/app.py`, `web/static/index.html`): `GET /api/system/modules` reads `/proc/modules`, parses name/size/used-by/deps, returns sorted list. Dashboard card with count badge, live filter input, and table with networking modules (wireguard, cfg80211, mac80211, ath, brcm, rtl) highlighted blue. On-demand Refresh button, loaded on page init.
- **WireGuard config QR export** (`web/app.py`, `web/static/index.html`): `GET /api/vpn/wireguard/config/qr` reads `/etc/wireguard/wg0.conf`, generates QR PNG via `qrencode` in a temp file, returns `image/png` with `Cache-Control: no-store`. `GET /api/vpn/wireguard/config/text` returns sanitized config with `PrivateKey` redacted. Dashboard card loads QR as blob URL (auth-gated), toggleable config text view in `<pre>` block. On-demand only.
- **System temperatures** (`web/app.py`, `web/static/index.html`): `GET /api/system/temps` globs all `/sys/class/thermal/thermal_zone*` sysfs entries, reads temp (÷1000 for °C), type, and trip_point_0_temp for critical threshold. Also calls `vcgencmd measure_temp` and `vcgencmd get_throttled` (Pi-specific). Classifies each sensor as `ok` (<70°C), `warm` (70-80°C), `hot` (>80°C). Dashboard card with colour-coded per-sensor rows and red throttling warning banner. Auto-refreshes on poll.

## [2.35.0] - 2026-05-25

### Added

- **USB device inventory** (`web/app.py`, `web/static/index.html`): `GET /api/system/usb` runs `lsusb`, parses bus/device/vendor-ID/product-ID/description via regex, filters root hubs and Linux Foundation entries. Dashboard card with Bus/Device/ID/Description table; Ethernet/RNDIS/ECM adapters highlighted green, modem/LTE/Sierra/Huawei/ZTE devices highlighted blue. On-demand Refresh button, loaded on page init.
- **System entropy pool** (`web/app.py`, `web/static/index.html`): `GET /api/system/entropy` reads `/proc/sys/kernel/random/entropy_avail` and `poolsize`, computes percent full, detects RNG source (`hardware_rng` via `/sys/bus/platform/drivers/bcm2835-rng`, `hwrng` via `/dev/hwrng`, else `software`), tests `os.getrandom(32, GRND_NONBLOCK)` for pool health. Dashboard card with large bit-count, colour-coded progress bar (green ≥75%, orange ≥40%, red <40%), source badge (blue=hardware, grey=software), getrandom tick/cross. Auto-refreshes on poll.
- **Top processes** (`web/app.py`, `web/static/index.html`): `GET /api/system/proctop` parses `ps aux --no-header`, extracts user/pid/cpu%/mem%/state/command-basename, returns top 10 by CPU% and top 10 by MEM% plus total process count. Dashboard card with side-by-side flex tables (By CPU / By Memory); CPU >10% highlighted orange, >50% red; MEM >10% highlighted orange. Auto-refreshes on poll.

## [2.34.0] - 2026-05-25

### Added

- **Active connections viewer** (`web/app.py`, `web/static/index.html`): `GET /api/network/connections` runs `ss -tunatp`, filters loopback and TIME-WAIT entries, extracts process names from the `users:` field. Returns proto, state, local address, peer address, and process. Dashboard card with colour-coded state badges (green=ESTABLISHED, blue=LISTEN, orange=SYN states). Auto-refreshes on poll.
- **Tailscale exit node status** (`web/app.py`, `web/static/index.html`): `GET /api/vpn/tailscale/exitnode` parses `tailscale status --json`, reads `ExitNodeStatus` from Self for the active exit node, and iterates Peer map for all peers with `ExitNodeOption=true`. Returns current exit node (hostname, IP, online status) and available peer list sorted by active→online→offline. Dashboard card highlights active node with globe icon; lists available peers with ACTIVE badge and online/offline colour. Auto-refreshes on poll.
- **Cron jobs viewer** (`web/app.py`, `web/static/index.html`): `GET /api/system/cron` reads `/etc/crontab`, all files in `/etc/cron.d/`, and runs `crontab -l` for `root` and `travel-router` users. Parses schedule, optional user field (cron.d entries), and command (truncated at 120 chars), groups results by source file. Dashboard card shows per-source tables with schedule in monospace blue, command column, user column when present. Refresh button for on-demand reload.

## [2.33.0] - 2026-05-25

### Added

- **Login history** (`web/app.py`, `web/static/index.html`): `GET /api/system/logins` runs `last -n 30 -F` (with `-F`-less fallback), filters system pseudo-logins (reboot/shutdown/LOGIN), cross-checks `who` to mark currently active sessions, returns user, TTY, source host, date string, and active flag. Dashboard card with active-session count badge and green bullet for active users. Auto-refreshes on poll.
- **Network interface statistics** (`web/app.py`, `web/static/index.html`): `GET /api/network/iface/stats` reads `/proc/net/dev`, parses all 16 fields per interface (RX/TX bytes, packets, errors, drops; TX collisions), skips loopback, sorts by total traffic. Uses existing `_fmt_bytes()` helper. Dashboard card with per-interface table, RX in blue, TX in green, error/drop counts in red when non-zero. Auto-refreshes on poll.
- **System update checker** (`web/app.py`, `web/static/index.html`): `GET /api/system/updates` runs `apt list --upgradable`, parses package name, current version, new version, and suite, detects security updates via `"security" in suite`, reads `/var/cache/apt/pkgcache.bin` mtime for cache age. Dashboard card shows ✅ up-to-date or update count with red security badge; security packages highlighted in red. Auto-refreshes on poll.

## [2.32.0] - 2026-05-25

### Added

- **Route table viewer** (`web/app.py`, `web/static/index.html`): `GET /api/network/routes` runs `ip -4 route show` and `ip -6 route show`, parses each line into destination, gateway, interface, proto, and metric fields, filters IPv6 link-local and loopback routes. Dashboard card with grouped IPv4/IPv6 sub-tables; default routes highlighted in accent colour. Auto-refreshes on poll.
- **WireGuard peer health** (`web/app.py`, `web/static/index.html`): `GET /api/vpn/wireguard/peers` reads `wg show all dump` (tab-separated 9-column format), computes last-handshake age in seconds, classifies each peer as `recent` (<3 min), `stale` (<10 min), or `idle`/`never`. Returns pubkey short form, endpoint, allowed IPs, RX/TX in human-readable form. Added `_format_duration()` and `_fmt_bytes()` module-level helpers. Dashboard card with colour-coded status badges. Auto-refreshes on poll.
- **Monthly data cap tracker** (`web/app.py`, `web/static/index.html`): `GET /api/network/datacap` reads `MONTHLY_CAP_GB` from `/etc/default/travel-router`, queries `vnstat --json m` for current month RX/TX on the busiest interface, computes percent used and GB remaining. Dashboard card with colour-coded progress bar (green/orange/red at 70%/90%) when cap is set, plain GB totals otherwise. Auto-refreshes on poll.

## [2.31.0] - 2026-05-25

### Added

- **Connected clients ARP table** (`web/app.py`, `web/static/index.html`): `GET /api/network/clients` parses `/proc/net/arp`, resolves hostnames via `getent hosts`, enriches with dnsmasq lease file (tries three common paths), and adds OUI vendor hints for known MAC prefixes (Raspberry Pi, Apple, Intel, Google, VMware, VirtualBox). Dashboard card with IP/MAC/Hostname/Vendor/Interface table. Auto-refreshes on poll.
- **VPN kill switch status** (`web/app.py`, `web/static/index.html`): `GET /api/vpn/killswitch` detects active kill switch via nftables default-drop policy, iptables FORWARD/OUTPUT chain policy, `travel-router-killswitch` systemd service, and `/etc/default/travel-router` KILL_SWITCH config key. Returns enabled status, detection method, and detail list. Dashboard card with ENABLED/DISABLED badge and detail list. Auto-refreshes on poll.
- **Log summary and export** (`web/app.py`, `web/static/index.html`): `GET /api/logs/summary` queries journald for last 1000 lines at warning level and above, counts errors/warnings, extracts up to 5 recent error lines, and lists failed systemd units. `GET /api/logs/export` streams a downloadable text bundle of journald (500 lines), per-service logs (100 lines each for 6 services), and syslog (200 lines). Dashboard card shows error/warning/failed-service counts with recent errors panel and Export button. Loaded on page init.

## [2.30.0] - 2026-05-25

### Added

- **SSH authorized keys viewer** (`web/app.py`, `web/static/index.html`): `GET /api/system/ssh/keys` reads `authorized_keys` for `root`, `pi`, and `travel-router` users, parses key type, comment, and fingerprint tail (last 16 chars of base64). Dashboard card shows table with color-coded type badges (green=ed25519, orange=rsa, purple=ecdsa). Loaded on page init.
- **DNS resolver config and latency** (`web/app.py`, `web/static/index.html`): `GET /api/dns/resolvers` parses `/etc/resolv.conf` nameservers, probes each with `dig +time=2 +tries=1`, and detects DoH provider from `/etc/dnsmasq.d/`. Dashboard card with OK/FAIL badges, latency color-coding (green <50ms, orange <200ms, red ≥200ms), sample response IP, and purple DoH badge when active. Loaded on page init.
- **Bandwidth history chart** (`web/app.py`, `web/static/index.html`): `GET /api/network/bandwidth` reads `vnstat --json` and returns hourly (last 24h, MB) and daily (last 30d, GB) RX/TX per primary non-loopback interface. Dashboard card with Hourly/Daily tab switcher and SVG bar chart (blue=RX, green=TX split per bar) with total summary. Auto-refreshes on poll.

## [2.29.0] - 2026-05-25

### Added

- **System resource usage** (`web/app.py`, `web/static/index.html`): `GET /api/system/resources` reads CPU% (sampled from `/proc/stat` with 200ms interval), memory used/total/available MB and percent (from `/proc/meminfo`), load averages 1/5/15min (`/proc/loadavg`), and uptime (`/proc/uptime`). Dashboard card with proportional usage bars (green→orange→red at 70%/90%), load average color-coding, and human-readable uptime. Auto-refreshes on poll.
- **Firewall rules viewer** (`web/app.py`, `web/static/index.html`): `GET /api/network/firewall` tries nft JSON (`nft -j list ruleset`) → nft plain text → iptables -L fallback. Returns rules grouped by table/chain with rule count and tool label. Dashboard card displays grouped monospace rule blocks. Loaded on page init.
- **Power control** (`web/app.py`, `web/static/index.html`): `POST /api/system/reboot` and `POST /api/system/shutdown` schedule the action 10 seconds out via a background daemon thread, returning `{"scheduled": true, "in_seconds": 10}`. Dashboard card with Reboot (orange) and Shutdown (red) buttons, browser confirm() guard, and live countdown display.

## [2.28.0] - 2026-05-25

### Added

- **CPU temperature history sparkline** (`web/app.py`, `web/static/index.html`): `GET /api/system/temp/history` samples CPU temp every 60 s via `vcgencmd measure_temp` (with `/sys/class/thermal` fallback) into a 60-sample ring buffer (1 hour). Dashboard card with current/min/avg/max stats and SVG sparkline with 65 °C warn and 80 °C danger threshold lines.
- **Internet speed test** (`web/app.py`, `web/static/index.html`): `GET /api/network/speedtest` tries three tools in order — `speedtest-cli --json`, Ookla `speedtest --format=json`, then curl Cloudflare download — and returns download/upload Mbps, ping ms, and server info. Dashboard card with on-demand run button and proportional speed bars.
- **WAN uplink status** (`web/app.py`, `web/static/index.html`): `GET /api/network/wan` parses `ip -j route` and `ip -j link` to enumerate known WAN interfaces (wlan1, usb0, bnep0, wwan0, eth1, ppp0), their operstate, default-route presence, routing metric, and gateway. Sorted active-first. Dashboard card with type icons, status badges (active/up/down), and metric/gateway columns. Auto-loaded on page init.

## [2.27.0] - 2026-05-25

### Added

- **Tailscale status and peers** (`web/app.py`, `web/static/index.html`): `GET /api/vpn/tailscale/status` runs `tailscale status --json` and extracts self-node info (hostname, DNS name, IPs, online state) and all peers (hostname, IPs, OS, online/offline badge, rx/tx bytes). Dashboard card with self-node summary bar and peers table sorted online-first.
- **System services status** (`web/app.py`, `web/static/index.html`): `GET /api/system/services` queries `systemctl is-active` and `is-enabled` for 12 key travel-router services (hostapd, dnsmasq, WireGuard, tailscaled, watchdogs, vnstat, etc.). Dashboard card with two-column grid and color-coded active/enabled badges.
- **AdGuard Home stats** (`web/app.py`, `web/static/index.html`): `GET /api/dns/adguard/stats` probes AdGuard Home REST API on ports 3000/80/8088, returns total query count, blocked count with percentage, average processing time, and top-5 blocked domains and clients. Dashboard card with 3-stat summary grid and top-domains/clients lists.

## [2.26.0] - 2026-05-25

### Added

- **WireGuard peer health** (`web/app.py`, `web/static/index.html`): `GET /api/vpn/wireguard/peers` runs `wg show all dump` and parses per-peer status (active/idle/stale/inactive based on 180s/600s handshake thresholds), human-readable handshake age, endpoint, allowed IPs, and formatted rx/tx bytes. Dashboard card with color-coded status badges (green=active, orange=idle, grey=stale).
- **DHCP leases viewer** (`web/app.py`, `web/static/index.html`): `GET /api/network/dhcp/leases` probes standard dnsmasq lease file paths and parses hostname, MAC, IP, and TTL with human-readable expiry labels (static/expired/Xs/Xm/Xh). Dashboard card with sortable table and lease source path footer.
- **Network interfaces overview** (`web/app.py`, `web/static/index.html`): `GET /api/network/interfaces` uses `ip -j addr` for address/MAC data and `ip -j -s link` for RX/TX stats. Returns all interfaces sorted by state (up first) with IPv4/IPv6 addresses and formatted byte counts. Dashboard card with state badges (green=up, red=down, grey=unknown).

## [2.25.0] - 2026-05-24

### Added

- **Listening ports scanner** (`web/app.py`, `web/static/index.html`): `GET /api/network/ports` runs `ss -tlnup` (with `netstat -tlnup` fallback) and parses protocol, local address, port, program name, and PID into structured JSON. Dashboard card with color-coded port badges (blue=SSH, green=HTTP/S, purple=DNS, orange=web UI) and sortable table. Loaded on page init.
- **Storage and USB device info** (`web/app.py`, `web/static/index.html`): `GET /api/system/storage` parses `df -h` output (skipping tmpfs/devtmpfs/overlay/squashfs) into disk partition objects with mount point, filesystem type, used/free/total in GB, and percent. Also parses `lsusb` for USB device list with vendor/product IDs and descriptions. Dashboard card with per-partition progress bars color-coded by usage (green/orange/red).
- **Ping connectivity checker** (`web/app.py`, `web/static/index.html`): `GET /api/network/ping?host=X&count=N` runs `ping -c N -W 3` with input validation. Dashboard card with quick-access buttons for Google DNS, Cloudflare, and router gateway; displays packet loss, min/avg/max RTT, and per-ping results table.

## [2.24.0] - 2026-05-24

### Added

- **System resource monitor** (`web/app.py`, `web/static/index.html`): `GET /api/system/resources` samples `/proc/stat` twice (0.5s apart) for CPU %, reads `/proc/meminfo` for memory, `/proc/uptime` for uptime, `/proc/loadavg` for load averages, and `vcgencmd measure_temp` / `/sys/class/thermal/thermal_zone0/temp` for CPU temperature. Dashboard card with CPU/memory progress bars, temperature color-badge (green/orange/red), and uptime+load grid.
- **Firewall rules viewer** (`web/app.py`, `web/static/index.html`): `GET /api/firewall/rules` parses `iptables -L -n --line-numbers` (filter table) and `iptables -t nat -L -n --line-numbers` (nat table) into structured JSON. Dashboard card with Filter/NAT tab switcher and color-coded targets (green=ACCEPT, red=DROP/REJECT, orange=MASQUERADE/SNAT/DNAT).
- **Wi-Fi connected clients** (`web/app.py`, `web/static/index.html`): `GET /api/wifi/clients` parses `iw dev wlan0 station dump` (wlan1 fallback) for connected station MAC, signal dBm (quality 0-100), rx/tx bytes, and inactive time. Dashboard card with signal strength bars (green/orange/red), human-readable byte counts, wired into `refreshAll()`.

## [2.23.0] - 2026-05-24

### Added

- **Apt update checker** (`web/app.py`, `web/static/index.html`): `GET /api/system/updates` runs `apt-get -s upgrade` dry-run and parses upgradable packages into name/old-version/new-version objects. Dashboard card with on-demand "Check Updates" button and package count.
- **Config backup downloader** (`web/app.py`, `web/static/index.html`): `GET /api/config/backup` streams a timestamped `.tar.gz` of `/etc/wireguard/`, `/etc/dnsmasq.conf`, `/etc/dnsmasq.d/`, `/etc/hostapd/`, `/etc/default/travel-router`, and `/var/lib/travel-router/`. Dashboard card with one-click download button.
- **Traceroute tool** (`web/app.py`, `web/static/index.html`): `GET /api/network/traceroute?host=X` runs `traceroute -n -m 20 -w 2` with automatic fallback to `tracepath -n`. Input validated against `[a-zA-Z0-9.\-:_]+`. Dashboard card with hostname input and hop-by-hop results.

## [2.22.0] - 2026-05-24

### Added

- **DNS lookup tool** (`web/app.py`, `web/static/index.html`): `GET /api/dns/lookup?host=X&type=A` runs `dig +short` with fallback to `nslookup`, returns records list. Dashboard card with hostname input, record type selector (A/AAAA/MX/TXT/CNAME/NS/PTR), and inline results display.
- **IP route table viewer** (`web/app.py`, `web/static/index.html`): `GET /api/network/routes` parses `ip route show` into dest/via/dev/metric/src fields, highlights default route in bold. Dashboard card with refresh button and full routing table.
- **ARP / neighbour table** (`web/app.py`, `web/static/index.html`): `GET /api/network/arp` parses `ip neigh show` into ip/mac/dev/state objects. Dashboard card with color-coded state column (green=REACHABLE, red=FAILED/INCOMPLETE).

## [2.21.0] - 2026-05-24

### Added

- **SSH login history** (`web/app.py`, `web/static/index.html`): `GET /api/system/logins` parses `last -n <limit> -w` for recent logins (user, tty, host, date, still-on/crashed status) and `lastb -n 10 -w` for failed login attempts. Dashboard "Login History" card shows a sortable table with color-coded active/failed rows.
- **Active DHCP leases** (`web/app.py`, `web/static/index.html`): `GET /api/dhcp/leases` parses `/var/lib/misc/dnsmasq.leases` (5-field format), computes human-readable remaining time with color-coding (red = expired, orange = <5 min). Dashboard card shows IP/MAC/hostname/expires columns sorted by IP.
- **DNS-over-HTTPS resolver selector** (`web/app.py`, `web/static/index.html`): `GET /api/dns/doh-resolver` reads current resolver from `/etc/default/travel-router` and systemd-resolved config. `POST /api/dns/doh-resolver` validates against `_DOH_RESOLVERS` dict (Cloudflare, Quad9, Google, NextDNS, AdGuard) and calls `set-doh-resolver.sh`. Dashboard card with dropdown and live apply button.

## [2.20.0] - 2026-05-24

### Added

- **WireGuard peer QR code & remove** (`web/app.py`, `web/static/index.html`): `GET /api/vpn/wireguard/peer/<pubkey>/qr` generates a client config template with the server's public key and returns a base64 PNG data URL via `qrencode`. `DELETE /api/vpn/wireguard/peer/<pubkey>` removes the peer block from `wg0.conf` atomically and calls `wg set wg0 peer <pubkey> remove` for live state. Dashboard peer list gains per-row QR (modal overlay) and Remove buttons.
- **ntfy notification config** (`web/app.py`, `web/static/index.html`): `GET /api/notify/config` reads `NTFY_TOPIC` and `NTFY_SERVER` from `/etc/default/travel-router`. `POST /api/notify/test` validates topic, fires a `curl` POST to the ntfy endpoint with a timestamped test message, returns the HTTP status code. Dashboard "🔔 Notifications" card shows current config with a Send Test button and inline result display.
- **Bandwidth quota status** (`web/app.py`, `web/static/index.html`): `GET /api/datacap/status` queries `vnstat --json m 1` for current-month rx+tx, computes percentage against the configured cap, and returns `over_alert`/`over_cap` flags. Dashboard data cap card gains a color-coded progress bar (green/orange/red) with threshold indicator and a Check Usage button. Also removed a duplicate datacap card from the HTML.

## [2.19.0] - 2026-05-24

### Added

- **Uplink history** (`web/app.py`, `web/static/index.html`, `install/10-finalize.sh`): Background daemon thread samples the active default-route interface every 60s, records transitions to `uplink-history.json` (max 200 entries) with interface type classification (wifi/ethernet/wireguard/vpn/usb-tether/bluetooth). `GET /api/uplink/history` returns newest-first with configurable limit. Dashboard card shows timestamp/interface/type/metric table with color-coded type column.
- **Pi throttle monitor** (`web/app.py`, `web/static/index.html`): `GET /api/system/throttle` calls `vcgencmd get_throttled`, decodes the bitmask into named flags for current (undervoltage, ARM freq cap, throttled, soft temp limit) and historical events, reads CPU temp and current frequency. Dashboard "Pi Health" card shows temp with green/orange/red threshold coloring, clean status or active flag list, and raw hex for diagnostics. Gracefully degrades to "not available" on non-Pi hardware.
- **Log export** (`web/app.py`, `web/static/index.html`): `GET /api/logs/export?lines=5000` reads up to 1000 lines from each known log file, falls back to `journalctl` if none found, returns a timestamped `.txt` file as a `Content-Disposition: attachment` download. Logs card gains an Export button that opens the URL in a new tab.

## [2.18.0] - 2026-05-24

### Added

- **Speedtest history** (`web/app.py`, `web/static/index.html`, `install/10-finalize.sh`): `_append_speedtest_result()` helper hooks into the existing `POST /api/system/speedtest` handler to persist each result (download_mbps, upload_mbps, ping_ms, server, method, timestamp) to `speedtest-history.json` (max 50 entries). `GET /api/system/speedtest/history` returns results newest-first. Dashboard card shows history table.
- **Reboot schedule manager** (`web/app.py`, `web/static/index.html`): `GET /api/system/reboot-schedule` reads `/etc/cron.d/travel-router-reboot` and calculates next reboot time. `POST /api/system/reboot-schedule` calls `schedule-reboot.sh` or writes the cron file directly. Dashboard card with enabled checkbox, hour/minute inputs, and Save button.
- **Network interface stats** (`web/app.py`, `web/static/index.html`): `GET /api/network/interfaces` parses `/proc/net/dev` for all non-loopback interfaces, reads `operstate` from `/sys/class/net/`, returns rx/tx bytes, packets, errors, and drops. Dashboard card with per-interface table showing RX/TX with human-readable sizes and error counts highlighted in red when non-zero.

## [2.17.0] - 2026-05-24

### Added

- **Static DHCP reservations** (`web/app.py`, `web/static/index.html`, `install/10-finalize.sh`): `GET/POST /api/dhcp/reservations` and `DELETE /api/dhcp/reservations/<mac>`. Validates MAC and IP, writes JSON store, regenerates `/etc/dnsmasq.d/99-travel-router-reservations.conf` with `dhcp-host=` entries, and reloads dnsmasq. Dashboard "📌 Static DHCP" card with table and inline add form.
- **Ping monitor** (`web/app.py`, `web/static/index.html`, `install/10-finalize.sh`): `GET /api/monitor/ping` pings all stored hosts (max 20) and returns `{host, up, latency_ms, label}`. `POST/DELETE /api/monitor/ping` manage the host list with shell-char validation. Dashboard on-demand card (not auto-refreshed) showing ● UP / ○ DOWN with latency column and per-host delete.
- **Tailscale exit node control** (`web/app.py`, `web/static/index.html`): `GET /api/tailscale/exit-node` returns current active exit node and list of peers with `ExitNodeOption`. `POST /api/tailscale/exit-node` calls `tailscale set --exit-node=<node>` (empty string clears). Dashboard card with status indicator, peer dropdown populated from live Tailscale status, and Apply button.

## [2.16.0] - 2026-05-24

### Added

- **LAN network scanner** (`web/app.py`, `web/static/index.html`): `GET /api/network/scan` auto-detects the AP subnet from `ip route show dev uap0`, runs `arp-scan --localnet` (up to 15s) with ARP table fallback, deduplicates by IP, and returns hosts sorted numerically. Dashboard "LAN Scanner" card with on-demand Scan button (not wired to auto-refresh) showing IP/MAC/Vendor table.
- **Service control panel** (`web/app.py`, `web/static/index.html`): `GET /api/services/status` calls `systemctl is-active` for 9 key router services and returns `{unit, state}` pairs. Dashboard card with color-coded status indicators (● active / ✖ failed / ○ other) and per-service Restart buttons that call the existing `/api/service/<name>/restart` endpoint.
- **Router config editor** (`web/app.py`, `web/static/index.html`): `GET /api/config/travel-router` reads 21 allowlisted keys from `/etc/default/travel-router`. `POST /api/config/travel-router` validates keys against allowlist, rejects newlines, and atomically rewrites the file preserving comments. Dashboard card renders an editable table with inline inputs; a Save Changes button appears on first edit.

## [2.15.0] - 2026-05-24

### Added

- **WiFi QR code card** (`web/app.py`, `web/static/index.html`): `GET /api/wifi/qr` reads SSID and WPA passphrase from `/etc/raspap/hostapd.ini` (falls back to `/etc/hostapd/hostapd.conf`), builds a `WIFI:T:WPA;S:...;P:...;;` string, and generates a PNG via `qrencode` with SVG fallback. Dashboard card shows scannable QR image with SSID label and raw WiFi string for guest joining.
- **AP info & channel selector** (`web/app.py`, `web/static/index.html`): `GET /api/wifi/ap-config` returns current SSID, channel, hw_mode, country code, and TX power. `POST /api/wifi/ap-config` validates channel (0=auto, 1–13 for 2.4 GHz, 36–165 for 5 GHz), atomically rewrites the hostapd config, and restarts hostapd. Dashboard card shows current AP config with a channel selector drop-down and Apply button.
- **Journald log viewer** (`web/app.py`, `web/static/index.html`): `GET /api/system/journal?unit=<name>&lines=<n>` runs `journalctl -u <unit> -n <lines> --no-pager --output=short-iso` against a whitelist of 13 travel-router service units. Dashboard card with service drop-down (10 units), line-count selector (50/100/200/500), scrollable `<pre>` output that auto-scrolls to the bottom.

## [2.14.0] - 2026-05-24

### Added

- **Scheduled tasks viewer** (`web/app.py`, `web/static/index.html`): `GET /api/cron/jobs` reads all `/etc/cron.d/travel-router-*` files, parses cron schedule fields, and converts them to human-readable descriptions (e.g. "Daily at 03:00", "Weekly on Sunday at 08:00"). Dashboard "Scheduled Tasks" card with table showing schedule, command name, and source file; Refresh button.
- **Client connection history** (`web/app.py`, `web/static/index.html`, `install/10-finalize.sh`): background daemon watches `/var/lib/misc/dnsmasq.leases` for changes (inotify-style polling every 5s), appending timestamped `{mac, ip, hostname, seen}` entries to `client-history.json` (capped at 500). `GET /api/clients/history` returns entries sorted newest-first with optional `?limit=` param. Dashboard "Client History" card shows last-seen table with relative timestamps.

## [2.13.0] - 2026-05-24

### Added

- **mDNS service browser** (`web/app.py`, `web/static/index.html`): `GET /api/mdns/services` runs `avahi-browse -all -t -r -p`, parses semicolon-delimited output, deduplicates on name+type+addr, returns up to 50 services sorted by type then name. Dashboard "Local Services (mDNS)" card with service type grouping, Scan button, and graceful "avahi-browse not available" degradation.
- **Tailscale peer map** (`web/app.py`, `web/static/index.html`): `GET /api/tailscale/peers` parses `tailscale status --json`, normalises peer fields across Tailscale versions. Dashboard card showing self node + peer table (Hostname/IP/Status/OS/Last Seen) with online/offline indicators, relative timestamps, and exit-node badge. Handles tailscale-not-running gracefully.
- **Software update checker** (`web/app.py`, `web/static/index.html`): `GET /api/update/check` queries GitHub releases API (cached 1 hour), compares semver tuples. `POST /api/update/apply` spawns `update-router.sh` in background + fires SSE event. Dashboard "Software Update" card with version info, release notes link, and Apply button; orange update badge in header when newer version exists.

## [2.12.0] - 2026-05-24

### Added

- **Firewall rule viewer** (`web/app.py`, `web/static/index.html`): `GET /api/firewall/rules` runs `iptables -L -n -v --line-numbers` for filter and nat tables, returning raw text + parsed chain dicts. Dashboard "Firewall Rules" card with Filter/NAT tab toggle, monospace scrollable `<pre>` block, refresh button, and last-updated timestamp.
- **Port forwarding manager** (`web/app.py`, `web/static/index.html`, `install/10-finalize.sh`): `GET/POST /api/portforward` and `DELETE /api/portforward/<id>`. Rules persist to `portforward.json`; applied via a custom `TRAVEL_PORTFWD` iptables chain (DNAT + ACCEPT). Dashboard card with rule table (Proto/Ext port/Internal/Comment/Delete) and inline add form with validation.
- **Log search and level filter** (`web/app.py`, `web/static/index.html`): `search=` and `level=` query params on `GET /api/logs`. Dashboard log viewer gains search text input, level dropdown (err/warning/info/debug), Search/Clear buttons, filtered-results banner, and highlighted match display using `<mark>` tags with safe HTML escaping.

## [2.11.0] - 2026-05-24

### Added

- **System resource monitor** (`web/app.py`, `web/static/index.html`): background daemon thread samples `/proc/stat`, `/proc/meminfo`, `os.statvfs('/')`, and `/sys/class/thermal/thermal_zone0/temp` every 30s, storing up to 120 entries. `GET /api/system/resources` returns current + 60-entry history. Dashboard card shows four SVG arc gauges (CPU%, RAM%, disk%, CPU temp) with green/yellow/red thresholds; auto-refreshes every 30s.
- **WiFi uplink scanner** (`web/app.py`, `web/static/index.html`): `GET /api/uplink/scan` runs `iw dev wlan0 scan`, parses SSID/BSSID/signal/security/channel into a sorted list (cap 20). `POST /api/uplink/connect` writes wpa_supplicant config atomically and runs `wpa_cli reconfigure`. Dashboard "Upstream WiFi" card with signal-strength bars, security badges, and inline connect form with password field.
- **LAN device alias manager** (`web/app.py`, `web/static/index.html`, `install/10-finalize.sh`, `scripts/travel-tui.py`): `GET/POST /api/clients/aliases` persists friendly names to `/var/lib/travel-router/aliases.json`. Clients table shows alias in bold with MAC below; ✏ pencil opens inline edit. TUI `AliasesScreen` (press `A`) lists, adds, and deletes aliases.

### Fixed

- `tests/unit/test_wg_key_rotate.bats`: wg pubkey mock now drains stdin (`cat > /dev/null`) before printing, preventing SIGPIPE on `tee` under bash `pipefail` that caused flaky test failures.

## [2.10.0] - 2026-05-24

### Added

- **Data-cap / monthly budget tracker** (`web/app.py`, `web/static/index.html`, `install/10-finalize.sh`): `GET/POST /api/datacap` reads and writes `/var/lib/travel-router/datacap.json` (cap_mb, iface, reset_day, baseline_bytes, reset_ts). Computes live used_mb from `vnstat`, remaining_mb, and pct_used; auto-resets monthly on reset_day. Dashboard Data Cap card shows a color-coded progress bar (green <75% / yellow <90% / red ≥90%) and inline set-cap form.
- **Wake-on-LAN panel** (`web/app.py`, `web/static/index.html`, `install/10-finalize.sh`): `GET/POST /api/wol/targets` manages `/var/lib/travel-router/wol-targets.json` (name/MAC/broadcast triples). `POST /api/wol/send` validates MAC with regex, sends 6×0xFF + 16× MAC magic packet via UDP broadcast using pure-Python stdlib socket. Dashboard WoL card with saved-target list and inline add-target form.
- **Latency history sparkline** (`web/app.py`, `web/static/index.html`): background daemon thread pings 8.8.8.8 every 60s using `ping -c1 -W2`, stores up to 60 samples in `_LATENCY_HISTORY`. `GET /api/latency/history` endpoint. Dashboard Latency card with inline SVG sparkline; color-coded latest value (green <20ms / yellow <50ms / red ≥50ms).

## [2.9.0] - 2026-05-24

### Added

- **Captive portal credential memory** (`web/app.py`, `web/static/index.html`, `scripts/travel-tui.py`): `GET/POST/DELETE /api/captive/credentials`; successful bypass auto-saves URL+username+password atomically. Dashboard pre-fills the bypass form with saved creds and shows "Last used: <hostname>" hint with a Forget link. TUI `[F]` key clears saved creds.
- **Traceroute card** (`web/app.py`, `web/static/index.html`, `scripts/travel-tui.py`): `POST /api/traceroute` validates target (hostname/IP regex, no shell metacharacters), runs `traceroute -n -m 15 -w 2`, returns per-hop `{hop, ip, rtts_ms}`. Dashboard Traceroute card with target input and color-coded hop table (green <20ms / yellow <100ms / red ≥100ms). TUI `TracerouteScreen` (press `T`).
- **USB/SD mount manager** (`scripts/mount-storage.sh`, `web/app.py`, `web/static/index.html`, `scripts/travel-tui.py`): `list`/`mount <device>`/`unmount` subcommands; device name validated `^[a-z0-9]+$`. `GET /api/storage` returns removable device list + mount status + df usage; `POST /api/storage/mount` / `POST /api/storage/unmount`. Dashboard Storage card with per-device Mount button and Unmount button. TUI `StorageScreen` (press `U`).

## [2.8.0] - 2026-05-24

### Added

- **Uplink signal quality card** (`web/app.py`, `web/static/index.html`, `scripts/travel-tui.py`): `GET /api/signal` parses `iwconfig`/`iw dev link` for WiFi quality%, dBm, SSID, channel; parses `mmcli` for LTE signal, operator, state (all fields nullable). Dashboard adds a 4th card in the top status row with color-coded quality bar (green ≥70%, yellow ≥40%, red <40%). TUI dashboard panel shows Unicode block-character bars.
- **DNS-over-HTTPS resolver selector** (`scripts/set-doh-resolver.sh`, `web/app.py`, `web/static/index.html`): preset choices (cloudflare/quad9/nextdns/adguard/system) or custom `https://` URL. Configures `systemd-resolved` when active. `GET/POST /api/doh`. Dashboard DNS Resolver card with `<select>` + custom URL input. `DOH_RESOLVER=system` default in config.
- **Scheduled daily reboot** (`scripts/schedule-reboot.sh`, `web/app.py`, `web/static/index.html`): `set HH:MM [--skip-if-clients]` / `clear` / `status` subcommands; writes `/etc/cron.d/travel-router-reboot`. `GET/POST /api/schedule/reboot`, `POST /api/uplink/reconnect` (best-effort wlan1 bounce + wpa_supplicant restart). Dashboard Scheduled Reboot card with time picker and skip-if-clients checkbox; Reconnect Uplink button.

## [2.7.0] - 2026-05-24

### Added

- **Bandwidth history sparkline** (`web/app.py`, `web/static/index.html`): background sampler thread reads `/proc/net/dev` every 5 min, persists up to 288 entries (24h) in `/var/lib/travel-router/bw-history.json`. `GET /api/bandwidth/history` endpoint. Dashboard bandwidth card replaced with pure inline SVG sparkline (rx=green, tx=blue); shows "Collecting data…" until 2+ samples exist.
- **Captive portal detection & auto-login** (`scripts/captive-check.sh`, `web/app.py`, `web/static/index.html`, `scripts/travel-tui.py`): probes Firefox detectportal + Google 204 on uplink, writes detection state to `/var/lib/travel-router/captive-portal.json`. `GET /api/captive`, `POST /api/captive/bypass` (curl form-submit, 15s timeout). Dashboard Captive Portal card with warning banner + auto-login form. TUI `CaptivePortalScreen` (press `C`).
- **SSE event notifications** (`web/app.py`, `web/static/index.html`, `scripts/travel-tui.py`): module-level `_event_queue` + `_push_event()` helper; `GET /api/events/stream` (SSE, 2s heartbeat, 5-min lifetime, token auth via `?token=` query param); `GET /api/events` polling fallback. Privacy change, QoS update, WireGuard peer add, and reboot events push toasts to the dashboard. Reboot event triggers 30s countdown in the header. TUI polls every 10s and shows modal for critical events.

### Fixed

- `scripts/ci-preflight.sh`: fixed SIGPIPE/exit-141 bug where `bats | tail` under `pipefail` caused the bats check to report failure even when all tests passed.

## [2.6.0] - 2026-05-23

### Added

- **Speed test** (`scripts/speedtest.sh`, `web/app.py`, `web/static/index.html`, `scripts/travel-tui.py`): tries speedtest-cli → Ookla CLI → curl/Cloudflare fallback; `GET /api/system/speedtest` returns cached result, `POST` runs a new test (90s timeout). Web UI Speed Test card shows color-coded Download/Upload/Ping tiles (green/yellow/red). TUI `SpeedTestScreen` (press `S`).
- **Config backup & restore** (`scripts/config-backup.sh`, `web/app.py`, `web/static/index.html`): `backup` subcommand collects `/etc/default/travel-router`, hostapd, WireGuard, privacy profiles into a `chmod 600` tar.gz with a safety pre-restore backup. `GET /api/system/backup` streams download; `POST /api/system/restore` accepts multipart upload. Backup & Restore card in web UI shows last-backup timestamp.
- **Per-device bandwidth limits** (`scripts/apply-qos.sh`, `web/app.py`, `web/static/index.html`, `scripts/travel-tui.py`): tc HTB + iptables MARK per MAC; supports `set`/`clear`/`list`/`clear-all`; limits persisted to `/var/lib/travel-router/qos-limits.json` and restored on firewall reload. `GET/POST /api/clients/qos`. Web UI client table gains Limit button per row with popover; throttled clients show badge. TUI `ClientsScreen` gains `q` binding → `QosModal`.
- `install/10-finalize.sh` updated to install `speedtest.sh`, `config-backup.sh`, `apply-qos.sh` to `/usr/local/sbin/`.

## [2.5.0] - 2026-05-23

### Added

- **Guest WiFi network** (`config/hostapd-guest.conf`, `systemd/hostapd-guest.service`): isolated second SSID (`TravelRouter-Guest`) on `uap1` virtual interface; guests get `192.168.5.0/24`, blocked from reaching primary AP clients and management ports (22, 8080). `ENABLE_GUEST_NETWORK=0/1` feature flag, optional `GUEST_PASS` (empty = open network).
- **Enhanced log viewer** (`web/app.py`, `web/static/index.html`): `GET /api/logs` gains `level`, `since`, `q`, `limit` query params for server-side filtering; `GET /api/logs/levels`; UI filter bar with level dropdown, text search (debounced), pause toggle, auto-scroll; log lines color-coded by severity.
- **Rich AP client table** (`web/app.py`, `web/static/index.html`): `GET /api/clients` returns hostname (from dnsmasq.leases), IP, signal (colored indicator), connected time, RX/TX bytes. Replaces bare MAC list.

### Fixed

- `tests/integration/test_web_api.py`: profile names corrected to `vpn-only/adblock-only/tor` (were `private/paranoid/standard`)
- `tests/integration/conftest.py`: patch `ACTIVE_PROFILE_FILE` + `APPLY_PROFILE_SCRIPT` to tmp stubs so privacy profile tests don't 503 in CI

## [2.4.0] - 2026-05-23

### Added

- **System Health card** (`web/static/index.html`): CPU temperature (color-coded green/yellow/red), uptime (human format), disk usage (progress bar + GB) — all live-polled from `/api/status`.
- **`GET /api/status`**: `system.cpu_temp_c`, `system.uptime_seconds`, `system.disk` (used_gb/total_gb/percent) fields added.
- **`GET /api/system/update-check`**: lightweight GitHub release check (no download); shows update badge in web UI on init.
- **`POST /api/system/diagnostic`**: runs `travel-diagnostic.sh` server-side; web UI shows spinner + scrollable monospace output panel.
- **Unit tests** (`tests/unit/`): 34 new bats tests for all v2.3 scripts — `test_wg_peer_expire.bats` (10), `test_apply_privacy_profile.bats` (9), `test_aide_check.bats` (7), `test_wg_key_rotate.bats` (8).
- **Unit tests** (`tests/unit/`): 16 new bats tests for watchdog scripts — `test_wireguard_watchdog.bats` (6), `test_tailscale_watchdog.bats` (5), `test_wan_watchdog.bats` (5).
- **README roadmap**: full v2.0–v2.3 shipped table (21 features), v2.4 up-next list, updated Management and wizard sections.

### Fixed

- `web/app.py`: removed duplicate route handlers for `DELETE /api/vpn/wireguard/peer` and `GET/POST /api/privacy/profile` introduced by integration-test agent; duplicate used wrong profile names (`standard/private/paranoid`).

## [2.3.0] - 2026-05-23

### Added

- **Security hardening** (`install/08-security.sh`): fail2ban jails for SSH and the web dashboard (5 failures in 60 s → 10-min ban); SSH hardening config (`MaxAuthTries 3`, `MaxStartups 3:50:10`, `LoginGraceTime 20`, `PermitRootLogin no`); monthly WireGuard key rotation timer; daily AIDE file integrity check with ntfy alert on changes.
- **`scripts/wg-key-rotate.sh`** + `systemd/wg-key-rotate.{service,timer}` — regenerates WireGuard keypair monthly, atomically patches `wg0.conf`, restarts `wg-quick@wg0` if active, notifies via ntfy with new public key.
- **`scripts/aide-check.sh`** + `systemd/aide-check.{service,timer}` — daily 03:00 AIDE integrity check; high-priority ntfy alert with truncated diff on any changes; exits 0 always.
- **`config/fail2ban/`** — fail2ban jail and filter configs for SSH + Flask web dashboard.
- **WireGuard peer management**: `DELETE /api/vpn/wireguard/peer/<pubkey>` removes peer from conf + live interface; `GET /api/vpn/wireguard/peer/<pubkey>/qr` returns PNG QR client config via qrencode; `/api/status` peers extended with `last_handshake_ago`, `rx_bytes`, `tx_bytes`.
- **`scripts/wg-peer-expire.sh`** + `systemd/wg-peer-expire.{service,timer}` — daily timer removes peers with `# expires: YYYY-MM-DD` comment past today's date.
- **Web UI peer management** (`web/static/index.html`): WireGuard peers table with Last Handshake/RX/TX columns, QR code modal, remove button with confirm dialog.
- **TUI peer management** (`scripts/travel-tui.py`): `WireGuardScreen` gains Expiry column, `d` to remove peer (confirm modal), `q` to display QR code in terminal.
- **Privacy profiles** (`config/privacy-profiles/`): four YAML profiles — VPN Only (kill switch on), Adblock Only, Tor (transparent proxy + DNS-over-Tor), Direct (auto-reverts after 10 min).
- **`scripts/apply-privacy-profile.sh`** — atomically switches VPN/Tor/DNS/firewall state; saves previous profile for revert; background auto-revert timer for Direct mode.
- **Privacy profile API** (`web/app.py`): `GET/POST /api/privacy/profile` — returns/sets active profile.
- **Web UI privacy card** (`web/static/index.html`): 2×2 profile selector with active highlight, wired into `refreshAll()`.
- **TUI privacy screen** (`scripts/travel-tui.py`): `PrivacyScreen` as first nav item (`P` key), active profile highlighted, applies in background worker.
- **Integration test suite** (`tests/integration/`): `test_failover.bats` (7 tests), `test_ota.bats` (5 tests), `test_web_api.py` (19 tests), `conftest.py` session fixture.
- **ARM64 native CI job** (`.github/workflows/unit-tests.yml`): `test-arm64` job on `ubuntu-24.04-arm` runs unit tests natively; `integration-tests` job added (non-blocking).

### Improved

- `web/app.py`: 5-second module-level status cache confirmed in place — prevents Pi Zero CPU spike under concurrent polling.

## [2.2.0] - 2026-05-23

### Added

- **`scripts/setup.sh`** — Pi one-liner bootstrap for fresh Raspberry Pi OS installs: `curl -fsSL .../setup.sh | sudo bash`. Detects architecture (aarch64/armv7l), reads `/proc/device-tree/model`, accepts any Debian/Raspberry Pi OS release by `ID=` rather than codename. Installs git, clones or updates `/opt/pi-travel-router`, then hands off to `install.sh`.
- **`scripts/flash.sh`** — one-liner SD card flash script: downloads latest GitHub release image, SHA256-verifies it, unmounts the target disk, writes with `dd` (using fast `/dev/rdiskN` on macOS), and ejects.
- **Tiered install TUI** (`install.sh`): three setup modes — **Quick** (WiFi only, security defaults on, country auto-detected from locale), **Standard** (adds remote access + security group), **Expert** (all 6 configuration groups). Passphrase confirm loop, SSID/country retry loops, locale-based country auto-detection (`LANG=en_GB.UTF-8 → GB`).
- **Existing-install detection** (`install.sh`): detects fully-installed (`/etc/travel-router-version`) and partially-installed (`/etc/default/travel-router`) states. Fully-installed menu: Upgrade / Reconfigure / Repair / Uninstall / Exit. Partial menu: Resume / Fresh start / Exit. Reconfigure re-prompts all vars with current values as defaults.
- **Pre-install summary card** (`install.sh`): box-drawn feature summary with explicit confirm before packages are installed.
- **USB gadget mode troubleshooting table** (README): per-OS instructions and common failure modes for the USB-ethernet gadget.

### Fixed

- `scripts/setup.sh` + `install.sh`: stdin EOF when invoked via `curl | sudo bash` — setup.sh re-attaches stdin to `/dev/tty` before `exec`ing install.sh, enabling interactive prompts through a pipe.
- `install.sh`: OS detection now uses the `ID=` field from `/etc/os-release` (accepts `debian` or `raspbian`) instead of matching codename — works on Bullseye, Bookworm, Trixie, and future releases without modification.
- `install/01-packages.sh`: `libimobiledevice6` renamed to `libimobiledevice-1.0-6` on Debian 13 (Trixie); package name probed at runtime via `apt-cache show`.
- `install/01-packages.sh`: log2ram APT repository pinned to `bookworm` suite for Trixie and later (upstream does not yet publish a Trixie suite).
- `scripts/ota-update.sh`: corrected boot-slot path references; CI coverage for OTA module restored.

### Improved

- `install.sh`: numbered section counter (`── [1] ──`, `── [2] ──` …) gives clear progress feedback during install.
- `install.sh`: post-install summary condensed to 3 numbered next-steps plus a useful-commands block.
- README Quick Start restructured into three options: Option A (pre-built image flash), Option B (Pi one-liner), Option C (Raspberry Pi Imager).

## [2.1.0] - 2026-05-08

### Fixed — Critical Reliability

- `scripts/ota-update.sh`: SHA256 verify decompressed image before `dd` write; aborts and skips `next-boot-slot` on mismatch; warn-only if no `.sha256` manifest (older releases remain compatible)
- `scripts/failover-watchdog.sh`: `can_reach_internet()` now 2-of-3 majority vote — HTTP generate_204 + HTTPS detectportal + DNS probe `@8.8.8.8`; single probe failure no longer demotes an uplink; timeout configurable via `FAILOVER_PROBE_TIMEOUT` (default 5s)
- `web/app.py`: WireGuard peer add re-reads `wg0.conf` after write to verify persistence; activates peer live via `wg addconf` if `wg0` interface is up; returns HTTP 500 on verification failure
- `web/app.py`: config editor validates `ENABLE_*` keys (must be 0 or 1), `*_PORT` keys (integer 1024–65535), `*TARGET`/`*ADDR`/`*SERVER` keys (no shell metacharacters) before write; invalid values return HTTP 400 with descriptive error

### Improved

- `web/app.py`: `/api/status` responses cached for 5 seconds — prevents Pi Zero CPU spike when multiple devices poll simultaneously
- `web/app.py`: `/api/system/reboot` now returns immediately with `{"rebooting": true, "in_seconds": 30}`; reboot executes after 30-second grace period allowing clients to disconnect cleanly

## [2.0.0] - 2026-05-08

### Added — Major Features (v2.0)

- **WireGuard VPN** (`scripts/wireguard-watchdog.sh`, `config/wg0.conf.template`): WireGuard raw VPN as first-class option alongside Tailscale; `ENABLE_WIREGUARD` flag; keypair generated at install; kill-switch chains updated for `wg0`; firstboot wizard gains VPN selector with 44-char base64 key validation
- **Web Management Dashboard** (`web/app.py`, `web/static/index.html`): Flask REST API on `:8080`; dark-theme single-page dashboard (no CDN, mobile-responsive); endpoints for status, logs, bandwidth, config, service restart, WireGuard peer add, OTA trigger; token auth (`/var/lib/travel-router/web-token`); AP-subnet clients unauthenticated
- **Python/Textual TUI** (`scripts/travel-tui.py`): 830-line async Textual app replacing bash TUI; DataTable for AP clients and WireGuard peers; 5s status auto-refresh; live log tail; route table view; feature flag toggles; bash TUI kept as `travel-tui-legacy` fallback
- **Modular install.sh** (`install/run.sh`, `install/00-validate.sh` … `install/10-finalize.sh`): 14-file modular architecture; `--dry-run`, `--module=X`, `--skip=X` flags; each module idempotent and independently sourceable; original `install.sh` unchanged as fallback
- **Test Suite** (`tests/unit/`, `.github/workflows/unit-tests.yml`): 34 bats tests across 4 scripts (captive-check, failover-watchdog, firewall, ups-monitor); 18 pytest tests for firstboot server (Content-Length, SSH key dedup, WireGuard validation); CI workflow on push/PR
- **OTA A/B Safety** (`scripts/ota-update.sh`, `scripts/ota-commit.sh`, `scripts/ota-rollback.sh`): downloads + GPG-verifies release, writes to inactive slot; `ota-commit.timer` marks slot permanent 5min after stable boot; `ota-rollback` reverts slot; web UI endpoint `/api/system/ota-update`
- **Captive Portal v2** (`config/portals/*.yaml`, `install/lib/portal_login.py`): 5 YAML portal templates (Marriott, Hilton, airport, Starbucks, generic); Python cookie-jar multi-step login with template matching and generic POST fallback; community-extensible
- **4G/LTE Modem** (`scripts/modem-watchdog.sh`, `config/91-usb-modem.rules`): ModemManager integration; udev rules for Sierra/Quectel/Huawei/ZTE; `wwan0` uplink at metric 150; APN config via feature flags
- **IPv6 First-Class** (`config/radvd.conf`, `config/dhclient6.conf`, `config/nm-dispatcher/70-dhcpv6-uplink.sh`): ip6tables save/restore persistence; DHCPv6 client on WAN interfaces via NM dispatcher; SLAAC (radvd) on `uap0` AP; IPv6 selectively re-enabled on AP while uplinks remain gated by kill-switch
- **Observability Stack** (`install/lib/logger.sh`, `systemd/travel-router-log-rotate.*`): structured `log_info/warn/error/debug` helpers writing JSON-compatible lines to `/var/log/travel-router/combined.log`; ntfy severity levels (critical→urgent, warning→high, info→default); daily log rotation

### Added — Config Engine
- `install/lib/config.py`: atomic Python config read/write with history log at `/etc/travel-router/history.log`; CLI shim `python3 config.py get KEY`
- `install/lib/common.sh`: shared `log`, `warn`, `die`, `section`, `run_or_dry`, `install_file` helpers

### Added — Packages
- `wireguard-tools`, `modemmanager`, `radvd`, `python3-flask`, `python3-textual` added to pi-gen stage packages

## [1.11.0] - 2026-05-07

### Fixed — Critical / Security
- `scripts/travel-router-firewall.sh`: removed unconditional `iptables -A FORWARD -i tailscale0 -o uap0 -j ACCEPT` that sat outside both kill-switch branches; both branches already add this rule, the extra copy accumulated a duplicate on every script restart and caused IPv4/IPv6 FORWARD chains to diverge
- `scripts/travel-router-firewall.sh`: added `enx+` to IPv6 non-kill-switch uplink FORWARD loop (was missing vs the IPv4 loop); IPv6 AP clients had broken forwarding on iPhone USB-Ethernet tether (enxXXXXXX) while IPv4 worked fine
- `scripts/travel-router-firewall.sh`: added `ip6tables INPUT -i uap0 -p tcp --dport 22/80 DROP` rules mirroring IPv4 INPUT rules; IPv6 AP clients could previously reach Pi admin ports (SSH, firstboot HTTP) directly
- `scripts/apply-split-tunnel.sh`: moved `tailscale0` existence check to before the `ip rule add fwmark 0x2 lookup 200` block; previously the rule was installed first, then exit 1 fired if tailscale0 was absent — leaving a blackhole routing rule in the kernel that silently dropped all domain-matched split-tunnel traffic
- `scripts/ups-monitor.sh`: added guard for API-reported `0%` as a parse artifact — when PiSugar API returns `null` in the `data` field, awk collapses it to `0`, which passed all numeric guards and triggered immediate shutdown; API-sourced `0%` now clears `pct` and falls through to sysfs (sysfs-reported `0%` is still a legitimate shutdown trigger)
- `build/config`: removed `WPA_COUNTRY=''` which caused `stage2/02-net-tweaks/01-run.sh` to call `raspi-config nonint do_wifi_country ""` inside the chroot — an empty country code is not in `iso3166.tab`, so `raspi-config` returned exit 1 silently under `bash -e`, causing every Build Pi Image CI run to fail with no visible error output

### Fixed — Reliability / Correctness
- `scripts/tailscale-watchdog.sh`: added `| select(. != null)` to peer-hostname jq filter; null hostnames (peers with no HostName field) previously entered the comparison list and generated spurious "Tailscale peer lost: null" alerts
- `scripts/update-blocklists.sh`: replaced deprecated `datetime.datetime.utcnow()` with `datetime.datetime.now(datetime.timezone.utc)` for Python 3.12+ compatibility
- `firstboot/server.py`: wrapped `int(Content-Length)` in `try/except ValueError` → returns 400; a malformed `Content-Length` header previously raised an unhandled exception that closed the connection without an HTTP response (denial-of-service)
- `firstboot/server.py`: SSH key deduplication changed from substring match (`pubkey in existing`) to line-exact match; a key whose blob appeared as a substring of another key was incorrectly suppressed and never added to `authorized_keys`
- `firstboot/server.py`: broadened `ANSI_RE` from `\x1b\[[0-9;]*m` to `\x1b\[[0-9;]*[A-Za-z]` to strip all CSI escape sequences (cursor movement, erase, etc.), not just SGR colour codes
- `install.sh`: `/etc/hosts` hostname substitution now uses `tempfile.mkstemp` + `os.replace` atomic write; the previous `open('/etc/hosts','w')` truncated the file before writing, risking an empty `/etc/hosts` on power loss during setup
- `scripts/travel-tui.sh`: replaced `chpasswd <<< "root:$pw"` here-string with `printf 'root:%s\n' "$pw" | chpasswd` pipe; here-strings create an FD-backed temp file visible in `/proc/<pid>/fd/` for the duration of the call
- `scripts/travel-tui.sh`: changed `cut -d= -f2` → `cut -d= -f2-` throughout config-value parsing; SSIDs or passphrases containing `=` characters were silently truncated at the first `=`
- `scripts/travel-tui.sh`: AP schedule timer units now restarted (`systemctl try-restart`) after `daemon-reload` when editing disable/enable times; previously the new `OnCalendar=` time only took effect after a reboot
- `scripts/travel-tui.sh`: version display now reads `/etc/travel-router-image-version` (the path written by `01-run.sh`) instead of `/etc/travel-router-version`; the version always showed "unknown" on images built from v1.0.0 onward

### Fixed — Configuration / Systemd
- `config/AdGuardHome.yaml`: reduced `upstream_timeout` from 10s → 5s (faster captive-portal detection); set `cache_ttl_min: 60` (prevents re-querying TTL-0 CDN records on every lookup); added DoH fallback (`https://cloudflare-dns.com/dns-query`) for hotel/corporate networks that block port 853
- `config/sshd-travel-router.conf`: added `AuthenticationMethods publickey`, `ClientAliveInterval 120`, `ClientAliveCountMax 3`, `AllowStreamLocalForwarding no`, `AllowUsers root` for defence-in-depth hardening
- `systemd/ap-disable.timer`, `systemd/ap-enable.timer`: changed `Persistent=true` → `Persistent=false`; `Persistent=true` caused a missed 02:00 disable-timer to fire immediately on next boot, cutting AP access during the morning
- `systemd/failover-watchdog.timer`: changed `AccuracySec=1s` → `AccuracySec=5s` to allow systemd timer coalescing and reduce unnecessary wake-ups on the Pi Zero 2 W
- `.github/workflows`: bumped all GitHub Actions to latest major versions (checkout v6, cache v5, setup-python v6, upload-artifact v7); supersedes Dependabot PRs #1–#4
- `.github/workflows/build-image.yml`: added `Validate build/config` step that fails fast with a clear error if `WPA_COUNTRY=''` ever reappears (empty string causes silent pi-gen stage2 failure)

## [1.10.0] - 2026-05-06

### Fixed — Critical / Security
- `scripts/travel-router-firewall.sh`: added `ip6tables KILL_SWITCH6` chain mirroring the IPv4 `KILL_SWITCH` chain; in the non-kill-switch path, added IPv6 FORWARD ACCEPT rules for all uplink interfaces and tailscale0 (CRITICAL: ip6tables FORWARD default policy is DROP with no rules, AP clients had zero IPv6 forwarding; with kill-switch enabled, IPv6 traffic bypassed the VPN entirely)
- `scripts/ups-monitor.sh`: removed the 0% battery guard introduced in v1.9.0 (CRITICAL regression: the non-numeric API artifact case is already caught by the `^[0-9]+$` regex guard; the extra 0% guard was preventing legitimate 0% shutdown from firing)
- `scripts/captive-check.sh`: fixed `form_action` regex character class — the previous class `[^"'\'' &gt;]+` treated `&gt;` as five literal characters `&`, `g`, `t`, `;`, `>`, inadvertently excluding the letters `g` and `t` from URL matches; virtually every portal URL was truncated at the first `t` or `g` (e.g. `/portal/auth` → `/por`); replaced with `[^ "'<>]+` (HIGH)
- `config/sshd-travel-router.conf`: added `PasswordAuthentication no` for defense-in-depth; `00-permit-root.conf` already sets this, but 99-travel-router.conf (deployed name) is now self-contained

### Fixed — Reliability / Correctness
- `scripts/travel-router-firewall.sh`: ERR trap now resets both `iptables` and `ip6tables` FORWARD policy to DROP on script failure
- `scripts/travel-router-firewall.sh`: added `flock -x` on `/run/lock/travel-router-firewall.lock` to prevent concurrent invocations accumulating duplicate INPUT/nat PREROUTING rules
- `scripts/wan-watchdog.sh`: captive-check.sh exit code now captured into `_cc_rc` before the `||` expression; previously `$?` always evaluated to the logger exit code (0) rather than captive-check's non-zero code
- `scripts/stop-tether.sh`: `notify-router.sh` call now guarded with `2>/dev/null || true`; without it, a notification failure prevented the subsequent `systemctl restart wan-watchdog.service` from running
- `scripts/failover-watchdog.sh`: `get_metric` now returns `"0"` (not empty) when a route has no explicit metric field; the empty return caused `promote_iface` to repeatedly re-set routes already at metric 0, causing unnecessary route churn every 60s
- `scripts/apply-split-tunnel.sh`: absent `tailscale0` now triggers `exit 1` instead of continuing with an empty routing table 200; previously `|| true` on `ip route replace` silently left split-tunnel routing broken with no error
- `scripts/notify-router.sh`: added `--fail` (`-f`) to curl; HTTP 4xx/5xx responses previously returned exit code 0 and were logged as successful deliveries
- `scripts/start-bt-tether.sh`: dhclient exit code capture changed from dead `PIPESTATUS[0]` read (unreachable under `set -euo pipefail`) to `DHCP_RC=0; ... || DHCP_RC=$?` pattern
- `scripts/tailscale-watchdog.sh`: jq `gsub` expanded to strip both `Z` and `+HH:MM`/`-HH:MM` timezone offsets before `fromdateiso8601`
- `scripts/start-tether.sh`: fallback for unavailable `systemd-run` now uses `nohup ... &` instead of inline execution to avoid blocking the udev event thread

### Fixed — Configuration / Systemd / TUI
- `systemd/adguard-home.service`: added `After=rc-local.service` and `Wants=rc-local.service`; AdGuardHome was frequently starting before `rc.local` created `uap0` (10.3.141.1), causing the HTTP UI bind to fail silently on every boot
- `scripts/update-blocklists.sh`: `mkdir -p /etc/nftables.d` moved before `mktemp`; previously the script crashed under `set -euo pipefail` if the directory was absent, before the EXIT trap could be set
- `scripts/travel-tui.sh`: AP Disable/Enable Time drop-in writes now use `mktemp`+`mv` atomic pattern; a partial write previously left a corrupt drop-in that silently disabled the timer permanently
- `scripts/travel-tui.sh`: HH:MM format validated before writing the timer drop-in; invalid values now print an error and skip the write
- `scripts/travel-tui.sh`: `_cfg_edit` Python call now guarded; `FileNotFoundError` on absent `/etc/default/travel-router` previously crashed the entire TUI session
- `build/stage-travel-router/files/imager-compat.sh` + `firstboot/server.py`: SSH key comment regex now strips trailing shell quote characters; Pi Imager-wrapped keys (`echo "ssh-ed25519 ... user@host"`) previously captured `user@host"` (with closing quote), causing duplicate entries in `authorized_keys`
- `install.sh`: Tor AP passphrase write to `hostapd.conf` now uses `tempfile.mkstemp` + `os.replace` atomic pattern (the main SSID/pass write was fixed in v1.9.0; this second write was missed)
- `firstboot/server.py`: `/retry` POST handler now returns HTTP 409 Conflict if an install is already running, preventing concurrent `install.sh` processes from corrupting system configuration

## [1.9.0] - 2026-05-06

### Fixed — Critical / Security
- `scripts/travel-router-firewall.sh`: added `ip6tables -P FORWARD DROP` + base IPv6 FORWARD rules (CRITICAL: ip6tables FORWARD default policy was ACCEPT, allowing AP clients with IPv6 addresses to bypass the VPN kill-switch and reach the WAN directly)
- `scripts/ups-monitor.sh`: PiSugar API `null` response now parsed as 0, which is guarded before the shutdown threshold check; avoids spurious `shutdown -h now` every 5 minutes during transient API init/charging-state blips (CRITICAL)
- `config/AdGuardHome.yaml`: bind `http.address` to `10.3.141.1:3000` instead of `0.0.0.0:3000`; the admin UI (unauthenticated on first boot) was reachable from hotel WiFi / WAN interfaces
- `systemd/adguard-home.service`: added `ProtectHome=yes`; AdGuard Home ran as root without home-directory sandboxing, giving it read access to `/root/.ssh/authorized_keys` and private keys
- `build/stage-travel-router/files/imager-compat.sh`: SSH public-key comment regex fixed — POSIX ERE `[^\\\n]` does NOT match a newline; it excludes the literal letter `n`, truncating comments like `nico@host` or `admin@router` and causing duplicate entries in `authorized_keys` when `server.py` re-adds the full-comment version (HIGH)

### Fixed — Reliability / Correctness
- `scripts/tailscale-watchdog.sh`: strip fractional seconds (`gsub("\\.[0-9]+Z$"; "Z")`) before `fromdateiso8601`; Tailscale emits RFC 3339 Nano timestamps (e.g. `2024-01-15T10:30:45.123456789Z`); jq's `fromdateiso8601` silently errored on them, meaning stale-handshake detection never fired in practice (HIGH)
- `scripts/travel-router-firewall.sh`: `save_rules()` now writes via `mktemp`+`mv` atomic pattern; direct `iptables-save >` could leave a truncated/corrupt rules file on power loss
- `scripts/apply-split-tunnel.sh`: `ip rule` idempotency grep anchored with `([^0-9]|$)` to prevent false match on `lookup 2001`, `lookup 2002`, etc.
- `scripts/captive-check.sh`: form action extraction now handles unquoted `action=/path` attributes (previously required quote chars immediately after `action=`)
- `scripts/notify-router.sh`: curl exit code now checked; failure is logged and exits 1 instead of silently swallowing delivery errors
- `scripts/start-bt-tether.sh`: `dhclient` exit code captured via `PIPESTATUS` and logged separately so lease-failure root cause is preserved through the pipe to `logger`
- `install.sh`: `hostapd.conf` SSID/passphrase substitution and `_safe_write_conf` (called ~15 times for `/etc/default/travel-router`) now use `tempfile.mkstemp` + `os.replace` atomic pattern; direct `open(path,'w')` truncated the file before writing, risking complete config loss on power loss
- `install.sh`: `AP_DISABLE_TIME` and `AP_ENABLE_TIME` validated as `HH:MM` in the direct-run path; wizard path already validated; direct invocation had no guard against newline injection into systemd drop-in files

### Fixed — Configuration / TUI
- `scripts/travel-tui.sh`: editing AP Disable/Enable Time in the Settings menu now regenerates the systemd timer drop-in (`/etc/systemd/system/ap-{disable,enable}.timer.d/time.conf`) and calls `systemctl daemon-reload`; previously the change was saved to `/etc/default/travel-router` but the running timer continued firing at the original time
- `scripts/travel-tui.sh`: `_ap_edit_ssid` now rejects SSID values containing `#`; hostapd parses `#` as a comment delimiter, silently truncating the broadcasted SSID
- `scripts/travel-tui.sh`: `_bw_delta` negative-delta guard changed from `2^32` wrap compensation to `0`-on-reset; Pi Zero 2 W runs arm64 with 64-bit (`u64`) byte counters that never wrap at `2^32`; the old formula produced a momentary fake ~4 GB/s spike in the dashboard on interface restart
- `scripts/failover-watchdog.sh`: `mkdir -p /run/lock` added before `exec 9>` flock; without it, if `/run/lock` does not exist the fd open fails silently and `flock -n 9 || exit 0` exits the entire watchdog
- `scripts/start-tether.sh`: `notify-router.sh` call guarded with `2>/dev/null || true` to prevent udev handler failure when ntfy server is unreachable
- `scripts/ap-schedule.sh`: added `-i uap0` to the `disable` branch of `hostapd_cli` for symmetry with the `enable` branch

## [1.8.0] - 2026-05-06

### Fixed — Critical / Security
- `config/sshd-travel-router.conf`: `PermitRootLogin no` → `prohibit-password` (CRITICAL regression: 99-travel-router.conf sorts after 00-permit-root.conf, so `PermitRootLogin no` overrode the correct `prohibit-password` setting in 00-permit-root.conf and locked root out of SSH entirely — the only account on the system)
- `scripts/travel-tui.sh`: `_ap_edit_pass` now rejects passwords containing `#`; hostapd silently treats `#` as a comment delimiter in its config file, meaning any password with `#` would be truncated to the preceding characters at runtime

### Fixed — Reliability / Correctness
- `scripts/apply-split-tunnel.sh`: `teardown_split_tunnel()` now deletes the iptables mangle rule before calling `ipset destroy vpn_domains`; the kernel refuses to destroy a referenced ipset, so the previous order caused silent teardown failures leaving stale routing marks
- `scripts/tailscale-watchdog.sh`: Go zero-time value `"0001-01-01T00:00:00Z"` (returned for peers that have never handshaked) now handled explicitly — `fromdateiso8601` would error on it; treated as epoch 0 (no handshake)
- `scripts/notify-router.sh`: added `--max-time 10 --connect-timeout 5` to curl; without a timeout the call blocks for ~2 min when WAN is down, stalling all callers
- `firstboot/server.py`: IPv6 address regex `_BARE_IP_RE` tightened to require ≥ 2 colon-separated groups; the previous pattern matched plain hex strings (`dead`, `cafe`), MAC addresses, and any alphanumeric token containing colons

### Fixed — Configuration / Systemd
- `systemd/failover-watchdog.timer`: removed `Requires=failover-watchdog.service` and `After=failover-watchdog.service`; `Requires=` on a timer triggers immediate service activation at boot, bypassing `OnBootSec=30` and running the watchdog before network interfaces are ready
- `systemd/wan-watchdog.timer`: same fix — removed `Requires=wan-watchdog.service`; previously bypassed `OnBootSec=60`
- `config/AdGuardHome.yaml`: reverted `http.address` from `127.0.0.1:3000` back to `0.0.0.0:3000`; the loopback binding introduced in v1.5.0 made the admin UI unreachable from AP clients at `10.3.141.1`; the AP firewall already limits external access
- `install.sh`: `"${TS_ARGS[@]:-}"` → `"${TS_ARGS[@]}"` — the `:-` fallback on a named array expands to one empty-string element instead of zero elements when the array is empty, passing a spurious `""` argument to `tailscale up`

## [1.7.0] - 2026-05-06

### Fixed — Critical / Security
- `scripts/travel-router-firewall.sh`: VPN kill-switch chain used `-j RETURN` for `tailscale0` traffic, which fell back to the FORWARD chain's DROP policy and silently killed all new Tailscale connections; changed to `-j ACCEPT`
- `firstboot/firstboot.service`: **regression fix** — `ProtectHome=yes` added in v1.6.0 made `/root` inaccessible, silently preventing SSH key writes to `/root/.ssh/authorized_keys` during wizard setup; replaced with `ProtectHome=read-only` + `ReadWritePaths=/root/.ssh`

### Fixed — Reliability / Correctness
- `scripts/tailscale-watchdog.sh`: `LastHandshake` field is an RFC 3339 string, not a Unix epoch; the previous jq arithmetic `$now - .LastHandshake` always emitted a type error (silently swallowed), meaning stale-handshake alerts were never sent; fixed with `fromdateiso8601`
- `scripts/captive-check.sh`: `restore_tailscale && rm -f STATE_FILE` aborted script via `set -e` on Tailscale auth expiry, losing success notifications; replaced with explicit if/else; protocol-relative form action URLs (`//host/path`) now handled correctly
- `scripts/failover-watchdog.sh`: `_tmp` in `_notify_uplink_change` declared `local` to prevent global scope leak
- `scripts/update-router.sh`: firewall-reload `diff` check compared the already-installed file against itself (always identical after the copy loop); replaced with `_fw_changed` flag set during the install loop so the firewall is actually reloaded when `travel-router-firewall.sh` changes
- `scripts/travel-tui.sh`: `_cpu_usage` denominator now includes `iowait`, `irq`, `softirq`, and `steal` ticks; previously the inflated denominator showed higher-than-actual CPU% on I/O-bound workloads; `MAX_BLOCKLIST_ENTRIES` display fallback corrected to `20000` (was `500000`)
- `install.sh`: forbidden-flag validation for `TAILSCALE_UP_ARGS` (`--authkey`, `--reset`, `--force-reauth`) added to direct-run path, matching the protection already in `server.py`

### Fixed — Configuration / Systemd / CI
- `config/91-android-tether.rules`: `DEVPATH!="*/gadget*"` guard added to `usb0` rules; the Pi's own `g_ncm` gadget interface has no `idVendor` in its sysfs parent chain so the `idVendor!=` guards were ineffective against it, causing `start-tether.sh` to fire on every boot
- `config/99-disable-ipv6-uplink.conf`: `net.ipv6.conf.default.disable_ipv6 = 1` removed; it was disabling IPv6 on all dynamically-created interfaces including `tailscale0`, breaking IPv6 subnet routing; per-interface lines for `wlan0`/`eth0` already handle uplink suppression
- `config/avahi-daemon.conf`: replaced invalid `nprocess-time-max=300` with `rlimit-nproc=10`; the former key is not recognised by avahi and was silently ignored
- `systemd/wlan-mac-random.service`: `ConditionPathExists=/sys/class/net/wlan0` added; service now skips gracefully on hardware without a `wlan0` interface
- `build/stage-travel-router/01-run.sh`: removed redundant `cleanup-rootpw.conf` drop-in; `firstboot.service` already carries the identical `ExecStartPost=` shred directive
- `.github/workflows/shellcheck.yml`: `config/rc.local` and `config/nm-wan-metrics` added to shellcheck coverage; `config/**` added to PR path trigger

## [1.6.0] - 2026-05-06

### Fixed — Critical / Security
- `start-bt-tether.sh`: gateway now captured BEFORE `ip route del default dev bnep0`; previously the route was deleted first, causing the capture to always return empty and Bluetooth PAN to never become a routable uplink
- `server.py`: `chpasswd` failure in `_spawn_install` now writes `FAIL_FILE` and exits 1 instead of silently creating `DONE_FILE`; `ROOTPW_FILE` is now always deleted (success or failure)
- `systemd/wan-watchdog.service` + `systemd/failover-watchdog.service`: removed `Restart=on-failure` / `RestartSec=10` — systemd silently ignores `Restart=` on `Type=oneshot` services; the round-6 addition had no effect

### Fixed — Reliability / Correctness
- `travel-router-firewall.sh`: `ENABLE_PER_DEVICE_VPN=0` now tears down the `VPN_DEVICES` mangle chain and `ip rule fwmark 0x64 lookup 100`; previously stale entries persisted routing marked traffic via Tailscale indefinitely
- `notify-router.sh`: `PRIORITY` parameter validated against known ntfy.sh values; unrecognised values fall back to `"default"` preventing HTTP header injection
- `start-bt-tether.sh`: `BT_MAC` validated as `([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}` before use
- `failover-watchdog.sh`: `set_default_metric` now uses `ip route replace` (atomic) instead of `del`+`add`, eliminating the routing gap window; `_UPLINK_STATE_FILE` written atomically via mktemp+mv
- `wan-watchdog.sh`: `STATE_FILE` (fail counter) written atomically via mktemp+mv
- `tailscale-watchdog.sh`: corrupt `ts-peers.json` now detected via `jq empty` and reset to `[]` baseline instead of silently discarding peer-loss events; lost peers now notified one per call instead of concatenated
- `ups-monitor.sh`: numeric guard added for `LEVEL` from `_get_battery_pct`; non-numeric sysfs content no longer causes arithmetic failure under `set -e`
- `stop-bt-tether.sh`: `ip route del default dev bnep0` added before `ip link set down` to prevent stale kernel routes
- `ap-schedule.sh`: `enable` branch now calls `_wait_hostapd` before issuing `hostapd_cli enable`, matching the `disable` branch
- `travel-router-firewall.sh`: `grep` pattern for `fwmark 0x64 lookup 100` anchored to prevent false positives on `lookup 1000` etc.
- `server.py`: `err_preseed` JSON on validation failure now built from an explicit allowlist; `TS_KEY` and `TOR_AP_PASS` are no longer included
- `server.py`: `_spawn_install()` wrapped in `try/except OSError`; temp files cleaned up and 500 returned on Popen failure
- `server.py`: `SPLIT_TUNNEL_DOMAINS` regex tightened to RFC 952 — rejects leading dots, leading/trailing hyphens per label
- `server.py`: `TAILSCALE_UP_ARGS` forbidden-flag check uses exact-match/`=`-prefix to avoid false positives
- `install.sh`: `ROUTER_HOSTNAME` regex updated to `[a-zA-Z0-9-]{0,61}` enforcing the 63-char RFC 952 label limit
- `install.sh`: `AP_PASS` / `TOR_AP_PASS` now reject `#` characters in all install paths
- `update-router.sh`: `chmod 755` applied to `${dest}.tmp` BEFORE `mv`, eliminating the window where a live script is non-executable; `tar` extraction adds `--no-absolute-names`; `VERSION_FILE` writes atomic via tmp+mv; `log()` uses `printf` not `echo`
- `update-blocklists.sh`: `NFT_NEW` created via `mktemp` and added to EXIT trap; no stale partial file left on failure
- `travel-tui.sh`: `uplink.state` value and `_bw_delta` iface parameter validated against `^[a-zA-Z0-9_.-]{1,15}$` before use in file paths; `_ap_edit_ssid` rejects SSID > 32 chars or containing control characters; Settings SSH key validated for key-type prefix; `tput reset` added after `bmon`/`iftop` to restore terminal state
- `travel-diagnostic.sh`: output directory validated as existent and writable before collection begins
- `build/stage-travel-router/files/imager-compat.sh`: `firstrun.sh` stub written atomically via mktemp+mv

### Fixed — Configuration / Systemd
- `config/AdGuardHome.yaml`: `anonymize_client_ip: true` — default config no longer logs full client IPs for all DNS queries
- `config/91-android-tether.rules`: `add` rules set `TAG+="pi_android_tether"`; `remove` rules match `TAGS==` instead of `ENV{ID_VENDOR_ID}` (unreliable at removal time)
- `config/hostapd.conf`: hardcoded `country_code=US` removed; operators must set the correct country code for their jurisdiction
- `systemd/generate-bandwidth-report.service`: `Wants=vnstat.service` added alongside existing `After=`
- `firstboot/firstboot.service`: `ProtectHome=yes` and `PrivateDevices=yes` added
- `systemd/ap-disable.service` + `ap-enable.service`: `After=NetworkManager.service network.target` added
- `systemd/wan-watchdog.timer` + `failover-watchdog.timer`: `After=<service>` added for correct ordering
- `systemd/tune-cake.timer`, `daily-digest.timer`, `generate-bandwidth-report.timer`, `vnstat-push.timer`: `RandomizedDelaySec=15min` added to prevent thundering-herd on multi-router deployments

## [1.5.0] - 2026-05-06

### Fixed — Critical / Security
- `travel-router-firewall.sh`: policy order corrected — `iptables -P FORWARD DROP` now set *before* `iptables -F FORWARD`; on the very first invocation (fresh boot with ACCEPT baseline) all packets were previously forwarded unfiltered during the flush window
- `update-router.sh`: `readonly REPO` (and other critical variables) added before sourcing `/etc/default/travel-router`; a compromised config file could previously override `REPO` and redirect updates to an attacker-controlled GitHub repository
- `install.sh`: WiFi QR code assembly now validates `AP_SSID`/`AP_PASS` for shell-unsafe characters (`\``, `$`, `(`, `)`) before construction; backtick/subshell injection in the MECARD string is no longer possible
- `build/stage-travel-router/01-run.sh` + `firstboot/firstboot.service`: `ExecStartPost` drop-in added to shred `/boot/firmware/root-password.txt` after firstboot completes; the FAT32 boot partition has no Unix permission enforcement and the plaintext password was readable by anyone with physical access to the SD card
- `config/AdGuardHome.yaml`: admin UI bound to `127.0.0.1:3000` instead of `0.0.0.0:3000`; previously reachable by anyone on the hotel network before a password was set

### Fixed — Reliability / Correctness
- `captive-check.sh`: `tailscale down 2>/dev/null || true` — non-zero exit from tailscale (daemon not running, already disconnected) no longer aborts the script under `set -e`, which previously left a stale portal-active state file and permanently disabled auto-login retries
- `tailscale-watchdog.sh`: `flock -n 9 || exit 0` guard added; concurrent invocations from rapid timer fires no longer race on state file writes
- `start-bt-tether.sh`: `bt-pan` PID captured and cleaned up via EXIT trap if setup fails; orphaned bt-pan processes no longer accumulate on repeated udev events
- `ups-monitor.sh`: `UPS_SHUTDOWN_THRESHOLD` now validated as integer with fallback warning; non-numeric config values no longer cause silent shutdown failure
- `notify-router.sh`: dead `python3` guard removed; the guard blocked all notifications on systems without python3 even though python3 is not used
- `wan-watchdog.sh`: `WAN_PING_TARGETS` now read into array to prevent glob expansion; `captive-check.sh` non-zero exit now logged
- `start-tether.sh` / `stop-tether.sh`: interface name validated against `^(enx[0-9a-f]+|rndis0|usb0)$` before use
- `ap-schedule.sh`: `_wait_hostapd` now probes with `hostapd_cli ping | grep PONG` instead of checking socket existence; stale socket files from a crashed hostapd no longer produce false-ready indications
- `generate-bandwidth-report.sh`: interface names and `$(date)` output HTML-escaped before embedding in report headings
- `vnstat-push.sh`: active interface from `uplink.state` validated against `^[a-zA-Z0-9_.-]{1,15}$` before appending to Pushgateway URL path
- `update-router.sh`: version string from GitHub API sanitized with `tr -cd 'A-Za-z0-9._-'`; `log()` uses `printf` instead of `echo` to avoid escape-sequence interpretation
- `update-blocklists.sh`: `curl` call now uses `--fail`; HTTP error responses (404/503) no longer silently treated as valid blocklist data
- `tune-cake.sh`: CAKE state file written atomically via tmp+mv
- `vnstat-metrics.sh`: Prometheus metrics file written atomically; node-exporter no longer sees truncated scrape files on parser failure
- `travel-tui.sh`: cleanup trap extended to `EXIT` so cursor is always restored; `read` calls guarded with `|| true`; MECARD QR escaping added for `;`, `,`, `"`, `\`, `:`
- `captive-check.sh`: `_probe()` temp file cleaned up on `RETURN` trap, preventing tmpfs accumulation

### Fixed — Configuration / Systemd
- `config/sshd-travel-router.conf`: `PasswordAuthentication no` added; installs via `install.sh` (not the pre-built image) previously left password SSH login enabled
- `config/avahi-daemon.conf`: `enable-wide-area=no`; previously leaked mDNS service names to hotel/upstream DNS infrastructure
- `config/91-android-tether.rules`: `ACTION=="remove"` rules now use `ENV{ID_VENDOR_ID}` instead of `ATTRS{idVendor}`; sysfs device attributes are often unavailable at removal time
- `config/nftables-travel-router.nft`: `ip6 dscp set cs0` rules added for uplink interfaces; IPv6 hotspot traffic was previously not DSCP-stripped, enabling carrier fingerprinting
- `systemd/adguard-home.service`: `NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectSystem=strict` added; AdGuard Home processes untrusted DNS queries and was running as root with no sandbox
- `systemd/failover-watchdog.service` + `systemd/wan-watchdog.service`: `Restart=on-failure` / `RestartSec=10` added; a single script error previously silently skipped the watchdog cycle
- `systemd/daily-digest.service`: `Wants=network-online.target` added alongside `After=` so systemd pulls the target into the transaction
- `firstboot/server.py`: `SPLIT_TUNNEL_DOMAINS` regex anchored and single-space-separated (no double-spaces producing `//`); `TAILSCALE_UP_ARGS` blocks `--auth`/`--reset`/`--force-reauth` flags; `/setup` Content-Length reduced to 32 KB, `/retry` to 8 KB
- `install.sh`: `wpa_supplicant.conf` now chmod 600 immediately after write; `ROUTER_TIMEZONE` validated before `timedatectl` call; `PasswordAuthentication no` write guarded with `grep -q` to prevent duplicates

## [1.4.0] - 2026-05-06

### Fixed — Critical / Security
- `server.py` / `install.sh`: `SSH_ADMIN_KEY` newline injection — multi-line values could inject extra keys into `/root/.ssh/authorized_keys`; both now strip `\n`/`\r` and validate the key starts with `ssh-`, `ecdsa-`, or `sk-`
- `travel-router-firewall.sh`: ERR trap added — if any `iptables` command fails under `set -e`, policy is restored to `FORWARD DROP` and the chain is flushed instead of leaving the firewall wide open with `ACCEPT` policy and no rules

### Fixed — Reliability / Correctness
- `stop-tether.sh`: `wan-watchdog.service` is now restarted (`systemctl --no-block restart`) after tether teardown so the watchdog re-evaluates uplinks immediately instead of routing through the dead interface
- `start-bt-tether.sh`: gateway capture now retries up to 5 times (1 s apart) before giving up; previously the route was queried before the interface was fully up
- `ups-monitor.sh`: `0%` battery is now a valid charge level — the `[[ pct -gt 0 ]]` guard that rejected it has been replaced with a `0–100` range check
- `tailscale-watchdog.sh`: removed `head -1` from stale-peer pipeline; all stale peers are now disconnected in one pass instead of only the first
- `install.sh`: `AP_PASS`/`TOR_AP_PASS` newlines stripped in non-interactive CLI path; previously only interactive input was sanitised
- `install.sh`: `_BARE_IP_RE` tightened to reject octets > 255 (e.g. `999.x.x.x`)
- `install.sh`: `ROUTER_HOSTNAME` regex updated to `^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$` — trailing hyphens (RFC 952 invalid) are now rejected
- `install.sh`: `_read_ap_ssid` now uses `shlex.split()` to re-parse the stored value; single quotes in SSIDs no longer garble the result
- `travel-tui.sh`: toggling `ENABLE_SPLIT_TUNNEL` / `ENABLE_BANDWIDTH_DASHBOARD` now calls `systemctl try-restart` / `systemctl stop` on the corresponding service so the change takes effect immediately without a reboot
- `travel-tui.sh`: large byte-counter arithmetic ported to `awk`; bash `$(( ))` overflow on 64-bit-signed-max values in GB range eliminated
- `update-router.sh`: `shopt -s nullglob` guards added around all glob-based `for` loops; a missing match no longer iterates over a literal glob string

### Fixed — Low / Housekeeping
- `ap-schedule.sh`: `_wait_hostapd()` helper added; `hostapd_cli` calls now wait up to 10 s for the control socket before acting
- `start-tether.sh` / `stop-bt-tether.sh`: `systemd-run --unit` names now include `$$` suffix to prevent name-collision errors when called in rapid succession
- `notify-router.sh`: `python3` presence now checked at startup; exits cleanly with a log message if not installed
- `ups-monitor.sh`: dead `&& pct -gt 0` condition removed from REST API path
- `travel-tui.sh`: UUOC — `cat uplink.state` replaced with `read -r … < uplink.state`
- `config/91-android-tether.rules`: added `ATTRS{idVendor}!="1d6b"` to exclude the Pi's own Linux Foundation USB gadget interface from triggering `start-tether.sh` on every boot
- `.github/workflows/shellcheck.yml`: permanently no-op "Shellcheck firstboot scripts" step removed (no `.sh` files exist in `firstboot/`)
- `.github/workflows/build-image.yml`: `if: always()` cleanup step added to unmount and remove leftover `MOUNTPOINT` temp directories on failure
- `README.md`: SSH section rewritten — accurately describes key-only authentication, how to add an SSH key, that password login is disabled by design, and that `/boot/firmware/root-password.txt` is for console access only

## [1.3.0] - 2026-05-07

### Fixed — Critical
- `apply-split-tunnel.sh`: fwmark mismatch in `teardown_split_tunnel` — teardown used decimal `1` while setup used hex `0x2`; routing rule persisted forever after disabling split-tunnel

### Fixed — Security / Correctness
- `captive-check.sh` `attempt_portal_login`: `ssid_slug` now strips `.` from allowed chars so an SSID named `..` can no longer produce path-traversal segments into parent directories
- `captive-check.sh` `_probe()`: `redirect_url` validated as `http*` before use; HTTP status code string can no longer be passed as a URL
- `server.py` `/setup`: double-submission guard — browser back+resubmit after install starts now redirects to `/status` instead of spawning a second concurrent `install.sh`
- `server.py` `_validate()`: `SPLIT_TUNNEL_DOMAINS` now validated with regex; shell-unsafe characters rejected at form submission time
- `server.py`: root password null-byte (`\x00`) check added — `chpasswd` is a C program that reads until null terminator, so a null in the validated password would silently shorten it
- `install.sh` interactive path: control-character guard added for `AP_SSID` and `AP_PASS` entered at the prompt
- `install.sh`: `ROUTER_HOSTNAME` validated against `^[a-z0-9][a-z0-9-]{0,62}$` before use in `sed` (direct-run path had no validation)
- `build/config`: `FIRST_USER_PASS` changed from plaintext `changeme` to a placeholder; `01-run.sh` now randomises the pi user's password before deletion

### Fixed — Reliability
- `notify-router.sh`: `curl` now ends with `|| true`; transport errors no longer propagate to callers under `set -euo pipefail`
- `start-tether.sh`: `notify-router.sh` call guarded with `|| true`; ntfy unreachability can no longer fail the tether service
- `start-bt-tether.sh`: `ip route add` for bnep0 guarded with `|| true`; a pre-existing route no longer short-circuits CAKE setup and notification
- `tailscale-watchdog.sh`: all 4 `jq` assignments restructured to `if ! var=$(jq ...)` — the previous `$?` guards were dead code under `set -euo pipefail` and would never execute on parse failure
- `ups-monitor.sh`: `THRESHOLD` validated as numeric immediately after loading from config; non-numeric value falls back to 10 with a warning instead of silently becoming 0
- `failover-watchdog.sh` `set_default_metric`: `ip route add` failure now logged instead of silently swallowed
- `wan-watchdog.sh` `truncate_log`: SC2015 `A && B || C` anti-pattern replaced with `if/else`; `mv` failure no longer triggers log data deletion
- `travel-tui.sh` `_cfg_edit`: Python inline now uses `shlex.quote()` to write config values; double-quotes in user-entered values no longer produce malformed shell syntax
- `travel-tui.sh` client table: `printf '%*s'` width capped at zero to prevent misalignment on hostnames/IPs longer than the column width

### Fixed — Monitoring / Observability
- `travel-diagnostic.sh`: secret redaction pattern changed to match to end-of-line; values containing spaces were previously only partially redacted
- `update-blocklists.sh`: `TMP_FILE` now uses `mktemp` with `trap … EXIT` for cleanup; fixed path eliminated TOCTOU symlink risk
- `update-router.sh`: tmpdir trap extended to `EXIT INT TERM`; portal scripts installation now checks against an explicit `PORTAL_ALLOWLIST`
- `vnstat-metrics.sh`: `TIMESTAMP` passed as `sys.argv[1]` instead of interpolated into Python heredoc; heredoc/pipe stdin conflict resolved

### Build / CI / Systemd
- `.github/workflows/shellcheck.yml`: `paths` filter moved off the tag trigger so shellcheck always runs when a release tag is pushed
- `systemd/adguard-home.service`: obsolete `syslog.target` dependency removed; `StandardOutput/StandardError=journal` added
- `systemd/generate-bandwidth-report.service`: `StandardOutput/StandardError=journal` added (consistent with peer services)
- `.github/workflows/build-image.yml`: `trap … EXIT` added to smoke-test and SBOM steps to clean up loop devices on failure

## [1.2.0] - 2026-05-07

### Fixed — Critical
- `firstboot.service`: removed `ProtectSystem=strict` + `ProtectHome=yes` which made `/etc`, `/usr`, and `/boot` read-only in the spawn namespace, causing every `install.sh` write to fail with EROFS; sandbox directives were incompatible with the child process that legitimately needs full filesystem access
- `wan-watchdog.sh` `truncate_log`: replaced fixed `.tmp` path with `mktemp` to eliminate race condition / zero-length log on crash

### Fixed — Security / Correctness
- `server.py`: `AP_SSID` now rejects control characters (embedded `\n` via crafted POST would have injected a new line into `hostapd.conf`)
- `server.py`: `AP_PASS` and `TOR_AP_PASS` now reject `#` (`hostapd.conf` treats `#` as comment start, silently truncating the passphrase)
- `install.sh`: WiFi QR string now escapes `\`, `;`, `,`, `:`, `"` per ZXing WPA spec so phone cameras can parse the code when SSID/pass contain these characters
- `install.sh` `_safe_write_conf`: switched from double-quoted `KEY="val"` to `shlex.quote()` (single-quoted) to prevent `$var` expansion when `/etc/default/travel-router` is sourced
- `build/stage-travel-router/01-run.sh`: `PasswordAuthentication` changed to `no`; the image comment already stated "key only" but the directive contradicted it
- `failover-watchdog.sh`: inline `ip route change` replaced with `set_default_metric` function call; `ip route change` fails silently when the route changed between check and action
- `captive-check.sh`: added `set -euo pipefail`; added `base_url` guard in `attempt_portal_login`; replaced fragile nested `eval "$_prev_trap"` trap restore with direct inline cookie jar cleanup
- `travel-router-firewall.sh`: `iptables/ip6tables -P FORWARD ACCEPT` now set immediately after flush so no packets are dropped during the rebuild window
- `start-bt-tether.sh` / `stop-bt-tether.sh`: `failover-watchdog.sh` now dispatched via `systemd-run --no-block` (same fix as `start-tether.sh`) to avoid blocking udev worker threads
- `update-router.sh`: `stop-tether.sh` and `vnstat-metrics.sh` added to `SCRIPT_ALLOWLIST` so they receive updates during auto-update runs

### Fixed — Reliability
- `install.sh`: `PUSHGW_URL`, `UPS_SHUTDOWN_THRESHOLD`, and `TAILSCALE_UP_ARGS` now persisted to `/etc/default/travel-router` via `_safe_write_conf` so wizard values survive reboot
- `firstboot.service`: added `Wants=imager-compat.service` alongside `After=` so systemd starts the compat shim when it exists
- `wan-watchdog.sh` recovery step 3: added `nmcli device connect wlan0` after `ip link set wlan0 up` to re-trigger STA association
- `failover-watchdog.sh` `set_default_metric`: skip `ip route add` if gateway is empty to prevent gateway-less default routes
- `apply-split-tunnel.sh` `teardown_split_tunnel`: delete the `iptables mangle PREROUTING` mark rule on teardown to prevent rule accumulation
- `ups-monitor.sh`: numeric regex guard before `-gt` comparison to avoid crash on non-integer API response under `set -euo pipefail`
- `notify-router.sh`: `${NTFY_TOPIC:-}` instead of `$NTFY_TOPIC` to avoid `set -u` unbound variable error
- `ap-schedule.sh`: both `hostapd_cli` calls now include `-i uap0` to target the correct VAP
- `tailscale-watchdog.sh`: `jq` parse errors restructured to call `exit 1` in the outer shell (not a no-op subshell)
- `generate-bandwidth-report.sh`: `logger` call fixed to avoid literal `%s` in syslog output
- `update-blocklists.sh`: blank lines excluded from `COUNT` so the sanity guard accurately reflects real IP entries
- `travel-tui.sh` `show_features`: `sed -i` replaced with inline Python atomic writer to safely handle `/`, `&` in flag values
- `vnstat-push.sh`, `tune-cake.sh`: useless `cat | tr` pipeline replaced with input redirection

### Build / CI
- `.github/workflows/build-image.yml`: `systemd/**` and `config/**` added to push/PR path triggers
- `build/stage-travel-router/01-run.sh`: removed redundant `-e` from shebang (superseded by `set -euo pipefail`)
- `config/travel-router-defaults`: added `AP_SUBNET` and `AP_GATEWAY` defaults for consistent reference across scripts

## [1.1.0] - 2026-05-06

### Fixed — Security (17 fixes)
- Image: random root password written to `/boot/firmware/root-password.txt`; `PermitRootLogin prohibit-password` in image build
- Wizard (`server.py`): CSRF token on `/retry`; preseed XSS fix (`</` → `<\/`); Host header allowlist (DNS rebinding prevention); SSID byte-length validation; AP_PASS printable-ASCII enforcement; UPS threshold 1–99 range check; PUSHGW_URL URL validation; 512-char cap on TAILSCALE_UP_ARGS; double-submit redirects to `/status`; log tail capped at 512 KB
- `travel-router-firewall.sh`: IPv6 FORWARD chain DROP policy; SSH blocked from `wlan0`; Prometheus (9100) and AdGuard (3000) blocked from `uap0`
- `install.sh`: log written mode 0600; BBR moved to `/etc/sysctl.d/99-bbr.conf`; `TAILSCALE_UP_ARGS` uses `read -ra`; WiFi QR redirected to `/dev/tty`; `PermitRootLogin prohibit-password` set unconditionally; open-WiFi fallback auto-enables kill switch

### Fixed — Reliability (25 fixes)
- `failover-watchdog.sh`: uplink state file always written on first active uplink; failed interfaces demoted to metric 900; DORMANT/UNKNOWN interface states detected; gateway read at action time not snapshot; route-del guarded by existence check; atomic log truncation via `mktemp`; `_notify_safe()` wrapper with logger fallback
- `wan-watchdog.sh`: dual HTTPS probe before declaring WAN down; `sleep 4` between disconnect/reconnect; confirmed `hostapd` stopped before `wlan0` down
- `captive-check.sh`: STATE_FILE moved to `/var/lib/travel-router/`; Tailscale restored + state file removed immediately after successful portal login; curl exit code checked; outer EXIT trap saved/restored in `attempt_portal_login`; `read -ra` for TAILSCALE_UP_ARGS
- `apply-split-tunnel.sh`: ipset teardown name fixed (`vpn_domains` consistent); `tailscale0` absence logged before route add
- `start-tether.sh`: failover-watchdog dispatched via `systemd-run --no-block`; UNKNOWN state accepted in poll; `TC_TETHER_BW` configurable (default 50mbit)
- `start-bt-tether.sh`: polls for `inet` address before proceeding; bt-pan liveness check after 2 s
- `stop-bt-tether.sh`: `nmcli disconnect` before `ip link down`; stale default route removed
- `clone-mac.sh`: hostapd stopped/restarted around MAC change; successful clone saved to `/var/lib/travel-router/cloned-mac`
- `ap-schedule.sh`: `enable` starts hostapd if not running; control socket checked before `hostapd_cli`
- `tailscale-watchdog.sh`: flock guard; configurable `TS_STALE_HANDSHAKE_SECS`; only alerts on peers with `TxBytes > 0`; exits 1 on daemon unreachable
- `ups-monitor.sh`: API response validated as integer 0–100; notify wrapped in `timeout 10`; shutdown hysteresis flag

### Fixed — TUI & Monitoring (15 fixes)
- `travel-tui.sh`: `if/else` replaces `&& B || C` for service toggle; `/run/travel-router/` for temp files; atomic `_cfg_edit` via `os.replace()`; SSID/passphrase edits use Python rewrite; `read` timeout vs closed-stdin distinguished; `chpasswd <<<` replaces `printf | chpasswd`; 32-bit counter wrap detection; `_cleanup clear` only on normal exit
- `travel-status.sh`: CPU from `/proc/stat` two-sample; `AP_IFACE` variable respected
- `travel-diagnostic.sh`: EXIT trap for temp-dir cleanup; stderr captured in `collect`; `HEADSCALE_URL`, `TOR_AP_PASS`, `PUSHGW_URL`, `IPHONE_BT_MAC` redacted
- `generate-bandwidth-report.sh`: atomic write via `.tmp`; HTML-escaped vnstat output
- `vnstat-push.sh`: single Python call per interface; active uplink included; PUSHGW_URL credentials redacted in logs
- `tune-cake.sh`: CAKE applied to `$UPLINK_IFACE` not hardcoded `wlan0`; `LC_ALL=C` for speedtest
- `daily-digest.sh`: `vnstat --json` + Python replaces `--oneline`; `AP_IFACE` variable
- `update-router.sh`: explicit script-name allowlist for `/usr/local/bin/` installs
- `update-blocklists.sh`: abort if blocklist < 100 entries; atomic rollback to `.prev` on `nft` failure

### Fixed — Build & CI (12 fixes)
- `00-packages`: pre-seeded 17 packages (`hostapd`, `dnsmasq`, `iptables*`, `jq`, `usbmuxd`, `libimobiledevice*`, `macchanger`, `vnstat`, `iw`, `qrencode`, `nftables`, `wireless-tools`, `bmon`)
- `build/config`: `WPA_COUNTRY=''` (install.sh is sole authority)
- `build-image.yml`: `softprops/action-gh-release` pinned to commit SHA; PR path trigger; image smoke-test step; SBOM generation; `workflow_dispatch` `git_ref` input; extended apt cache key
- `shellcheck.yml`: `ludeeus/action-shellcheck` pinned to commit SHA; `push: tags: v*` trigger
- `python-lint.yml`: switched from `pyflakes` to `flake8` + `pylint`
- `.github/dependabot.yml`: weekly github-actions dependency updates

### Fixed — Documentation (13 fixes)
- `build/stage-travel-router/01-run.sh`: random root password; `PermitRootLogin prohibit-password`; pi-user deletion assertion
- `build/README.md`: correct QEMU symlink commands
- `README.md`: Android tether metric corrected; RaspAP credential change warning
- `AGENTS.md`: nftables mangle attribution; `rndis0` interface row; `firstboot/` and `build/` in structure table
- `CHANGELOG.md`: duplicate entries removed; version comparison footer links
- `.github/CONTRIBUTING.md`: quad-core CPU fix; issue template creation URLs; dev deps note
- `CODE_OF_CONDUCT.md`: expanded from stub to proper conduct document
- `pull_request_template.md`: TUI coverage checklist item
- `.github/SUPPORT.md`: created
- `firstboot/README.md`: security note about unauthenticated `/status` log

### Added
- `firstboot/firstboot.service`: systemd sandbox (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`, capability bounding)
- `notify-router.sh`: `NTFY_TOKEN` bearer auth support; `set -euo pipefail`
- `wizard (index.html)`: 250-word passphrase list (5 words, ~39 bits); 12 new country options; inline mobile-friendly confirm; empty TAILSCALE_UP_ARGS default

## [1.0.0] - 2026-05-06

### Fixed — Security & Correctness (27 fixes)
- `captive-check.sh`: STATE_FILE touch moved before `tailscale down`; cookie jar uses `mktemp` with EXIT trap; SSID slug sanitised to `[a-zA-Z0-9._-]` capped at 64 chars; form `action=` extraction handles single-quoted and bare attributes; `|| true` on portal script source
- `travel-router-firewall.sh`: `iptables -P FORWARD DROP` moved after all ACCEPT rules to eliminate traffic blackhole window during startup
- `failover-watchdog.sh`: flock guard prevents overlapping runs; `get_gateway`/`get_metric` use awk string equality; boot-notification suppressed when no previous uplink; ICMP ping replaced with dual HTTP probe (gstatic + detectportal)
- `firstboot/server.py`: 409 on double-submit; `/retry` endpoint; CSRF token; `X-Frame-Options`, `X-Content-Type-Options`, `Content-Security-Policy` headers; content-type validation (415); strict UTF-8 decode (400 on error); hostname and time regex hardened; headscale URL validated; ntfy topic capped at 64 chars; spawn exit code captured explicitly
- `install.sh`: hostapd.conf `ssid=` and `wpa_passphrase=` written via Python (safe for `#` and spaces); all config substitutions use `_safe_write_conf` Python helper; Tor passphrase guard; hostname substitution via Python
- `apply-split-tunnel.sh`: `modprobe ip_set` guard; `teardown_split_tunnel()` when disabled
- `update-router.sh`: atomic `mv` for all script writes; tarball integrity check via `tar -tjf`; portal examples synced
- `build/stage-travel-router/01-run.sh`: `git clone --depth=50` with 5-attempt retry; `dtoverlay=dwc2` inserted under existing `[all]`; `imager-compat.sh` extracted from heredoc to `files/`

### Fixed — Hard Reliability (18 fixes)
- `travel-tui.sh`: `_cfg_edit` rewritten with Python heredoc (safe for `|`, `\`, newlines); `show_clients` converted from tail-recursion to `while true` loop; re-source config after SSH key edit
- `failover-watchdog.sh`: HTTP probe for internet reachability
- `notify-router.sh`: `--max-time 10`; NTFY_TOPIC URL-encoded via `urllib.parse.quote`
- `start-bt-tether.sh`: bt-pan PID saved to `/run/bt-pan.pid`; duplicate-start guard; `nmcli` DHCP with `dhcpcd` fallback
- `clone-mac.sh`: `--restore` uses `macchanger -p` (permanent hardware MAC, not random)
- `update-blocklists.sh`: `mv` before `nft -f` for correct persistence order
- `tailscale-watchdog.sh`: `command -v jq` guard
- `ups-monitor.sh`: sleep before shutdown increased to 20 s
- `stop-bt-tether.sh`: `set -euo pipefail`; calls `failover-watchdog.sh` on exit
- `build/stage-travel-router/00-packages`: added `python3`, `network-manager`
- `firstboot/firstboot.service`: `After=network-online.target`; `TimeoutStartSec=infinity`; `RequiresMountsFor=/opt`

### Fixed — Medium Priority (25 fixes)
- `travel-tui.sh`: dynamic flag index bounds check; `_bw_delta` uses elapsed time from timestamp file; WiFi password masked in QR flow; batched `systemctl is-active`; `AP_IFACE` variable; `_cpu_usage` reads `/proc/stat` twice
- `failover-watchdog.sh`: `_notify_uplink_change` requires non-empty previous uplink; `truncate_log` helper
- `wan-watchdog.sh`: STATE_FILE moved to `/var/lib/travel-router/`; captive-check only on wlan0; recovery restarts hostapd; `truncate_log` helper
- `firstboot/server.py`: `_load_preseed` sets `ROUTER_HOSTNAME`; `UPS_SHUTDOWN_THRESHOLD`/`PUSHGW_URL`/`TAILSCALE_UP_ARGS` in STRING_FIELDS; exceptions logged with traceback
- `firstboot/index.html`: `crypto.getRandomValues()` passphrase; country selector with 28 destinations; new advanced fields
- `install.sh`: AP schedule timer drop-ins interpolate `$AP_DISABLE_TIME`/`$AP_ENABLE_TIME`; CAKE service install; all new config keys persisted
- `generate-bandwidth-report.sh`: `shopt -s nullglob`; all tether interfaces reported (no early break)
- `start-tether.sh`: polling loop for `state UP` replaces `sleep 3`
- `ap-schedule.sh`: `systemctl is-active --quiet hostapd` guard
- `setup-2fa.sh`: TOTP secret print wrapped in `[ -t 1 ]` TTY check
- `tune-cake.sh`: reads uplink state file; passes `--interface` to speedtest-cli
- `travel-diagnostic.sh`: case-insensitive redaction; collects `/etc/travel-router-version`
- `vnstat-push.sh`: awk JSON replaced with Python

### Fixed — Low Priority / CI (14 fixes)
- `captive-check.sh`: hardened SSID slug
- `stop-bt-tether.sh`: strict mode
- `portals/example-credentials.sh`: `jq` dependency check with Python alternative note
- `log2ram` JOURNALD_AWARE update made idempotent
- `firstboot/server.py`: CSRF token validation; security headers
- `.github/workflows/shellcheck.yml`: pinned to `@2.0.0`; `continue-on-error` removed; firstboot step added
- `.github/workflows/python-lint.yml`: glob for `.py` files
- `build/stage-travel-router/files/imager-compat.sh`: extracted to standalone file for CI syntax checking
- `CODE_OF_CONDUCT.md`: added
- `CONTRIBUTING.md`: updated

## [0.9.1] - 2026-05-06

### Fixed
- Captive-portal failover-watchdog now uses a dual-endpoint HTTP probe instead of ICMP ping for reliable detection behind hotel NAT
- Wizard now includes fields for Bluetooth MAC address, bandwidth cap, and AP schedule times
- Wizard displays a weak-password warning when the AP passphrase is too short or common
- Per-SSID captive-portal script templates: drop a `.sh` named after the hotel SSID into `/etc/travel-router/portals/` for automatic re-authentication
- `travel-status` and TUI dashboard now display Wi-Fi RSSI when the active uplink is hotel/open WiFi
- TUI dashboard now shows AP client IP addresses alongside client count

## [0.9.0] - 2026-05-06

### Added
- Community health files: CONTRIBUTING, SECURITY policy, bug-report and feature-request issue templates, PR template
- Wi-Fi country selector added to the first-boot wizard
- CI build badge added to README
- Windows 10/11 CDC NCM USB note: inbox driver, no installation needed
- GitHub repository topics and description updated

## [0.8.3] - 2026-05-06

### Fixed
- Raspberry Pi Imager compatibility hardening: robust fingerprint detection, improved SSH key extraction, corrected file permissions
- USB gadget switched from `g_ether` to `g_ncm` for Windows 10/11 plug-and-play (inbox CDC NCM driver, no installation needed)
- CHANGELOG versioned: `[Unreleased]` split into `[0.8.0]`, `[0.8.1]`, and `[0.8.2]` sections

## [0.8.2] - 2026-05-06

### Fixed
- USB gadget enumeration: added `modules-load=dwc2,g_ncm` to `cmdline.txt` so the gadget interface comes up reliably on first boot
- Raspberry Pi Imager compatibility: neutralise `firstrun.sh` after consuming it and apply the SSH public key to the root account

## [0.8.1] - 2026-05-06

### Fixed
- CI image build: pinned pi-gen to `bookworm-arm64` branch to match `RELEASE=bookworm`
- CI image build: install `qemu-user-binfmt` alongside `qemu-user` (resolves package conflict with `qemu-user-static`)
- CI image build: add `qemu-arm` symlink for pi-gen binfmt check

## [0.8.0] - 2026-05-05

### Added
- Pre-built SD card image with GitHub Actions build pipeline (pi-gen based)
- First-boot web wizard served at `travelrouter.local` for guided initial setup
- Non-interactive install mode via `INSTALL_NONINTERACTIVE=1` env var
- Root-as-default-user with `changeme` default password (image-only)
- USB gadget mode pre-enabled in image so wizard is reachable via USB-C at `192.168.7.1` before install runs
- `travel-diagnostic` command: collects redacted logs, network state, and config into a timestamped tar.gz
- Wizard error recovery: install failures show last 50 log lines and a Retry button
- AP passphrase generator button in wizard (4-word random passphrase, no external dependencies)
- Raspberry Pi Imager pre-seed compatibility: wizard pre-fills hostname and SSH key from `firstrun.sh` if present

### Changed
- Image builds ship root-only login flow; password change enforced on first wizard submission
- `wan-watchdog.sh` recovery steps now use NetworkManager instead of the removed `dhcpcd`
- `update-router.sh` re-applies firewall rules when `travel-router-firewall.sh` changes in an update

## [0.7.0] - 2026-05-04

### Added
- nftables TTL/DSCP rule migration replacing legacy iptables shims (#1)
- Domain-based split tunnel for selective Tailscale routing (#19)
- SSH two-factor authentication (TOTP) (#27)
- WAN metric auto-management via NetworkManager dispatcher (#32)
- Bandwidth dashboard, Prometheus exporter, and vnStat push integration (#33 #34)
- Real-time traffic inspector built on bmon + iftop (#45)
- PiSugar 3 UPS monitor with battery telemetry (#48)
- Headscale self-hosted control server support (#46)
- Enhanced TUI dashboard with usability improvements

### Changed
- `ENABLE_WAN_METRICS` now defaults to `1` on fresh installs

### Security
- Stateful FORWARD policy with explicit KILL_SWITCH ordering
- CAKE bandwidth auto-tuning
- SSH hardening with optional pubkey-only authentication

## [0.6.0] - 2026-05-04

### Added
- Hardware watchdog, log rotation, daily digest reports, expanded README (#reliability)

## [0.5.0] - 2026-05-04

### Added
- Per-client bandwidth fairness queues
- Per-device Tailscale exit-node routing (#21 #44)

## [0.4.0] - 2026-05-04

### Added
- TUI dashboard, `status` command, MOTD branding
- Uplink alerts, AP schedule, Wi-Fi QR helper

## [0.3.0] - 2026-05-04

### Added
- Avahi reflector for cross-segment mDNS
- Tailscale watchdog
- Android USB tethering support
- AdGuard Home integration (#18 #24 #28 #35)

## [0.2.0] - 2026-05-04

### Added
- DNS-over-TLS upstream resolver
- VPN kill switch
- Unattended security updates (#16 #17 #26)

## [0.1.0] - 2026-05-04

### Added
- Initial public release: install.sh, scripts, systemd units, configs
- USB gadget mode and 15 baseline tweaks
- Auto-update system, captive portal auto-login, reproducible install
- AGENTS.md context document for AI-assisted contributors
- GL-MT3000 synergy guide
- CI workflow, `clone-mac.sh`, documentation refresh

### Fixed
- All shellcheck warnings cleared in baseline scripts

[Unreleased]: https://github.com/NicoMancinelli/pi-travel-router/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.9.1...v1.0.0
[0.9.1]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.8.3...v0.9.0
[0.8.3]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/NicoMancinelli/pi-travel-router/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/NicoMancinelli/pi-travel-router/releases/tag/v0.1.0
