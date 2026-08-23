# hostapd HT40 Bench Runbook (#3)

Decision procedure for roadmap item **#3 — hostapd HT Capability full review**.
Requires the physical Pi, one bench laptop (iperf3 server), and one WiFi client
(phone or laptop with iperf3). Budget ~45 minutes.

## Question being answered

Current config uses `[HT40][SHORT-GI-20][DSSS_CCK-40]` on channel 6. The brcmfmac
driver already rejected `[SHORT-GI-40]`, so the open question is whether **primary
+ secondary channel placement** (`[HT40+]` vs `[HT40-]`) buys real throughput on
this radio, or whether plain `[HT40]` should stay.

- `HT40+`: secondary channel **above** primary (channel 6 → uses 6+10)
- `HT40-`: secondary channel **below** primary (channel 6 → uses 2+6)

## 0. Baseline snapshot (before touching anything)

```bash
ssh <pi>
sudo cp /etc/hostapd/hostapd.conf /tmp/hostapd.conf.bak
grep -E "^channel|^ieee80211n|^ht_capab" /etc/hostapd/hostapd.conf
iw dev uap0 info          # note ctrl channel & width
iw dev uap0 station dump  # while client connected: note tx bitrate
```

Start iperf3 server on the bench laptop (wired to nothing — it talks over WiFi
as a client of uap0):

```bash
iperf3 -s
```

## 1. Variants to measure

| Variant | ht_capab | Notes |
|---|---|---|
| A (control) | `ht_capab=[HT40][SHORT-GI-20][DSSS_CCK-40]` | current deployed |
| B | `ht_capab=[HT40+][SHORT-GI-20][DSSS_CCK-40]` | secondary above |
| C | `ht_capab=[HT40-][SHORT-GI-20][DSSS_CCK-40]` | secondary below |
| D | `ht_capab=[HT20][SHORT-GI-20][DSSS_CCK-40]` | 20 MHz floor |

## 2. Per-variant procedure

```bash
sudo sed -i 's/^ht_capab=.*/ht_capab=<VARIANT>/' /etc/hostapd/hostapd.conf
sudo systemctl restart hostapd
sleep 5
iw dev uap0 info   # confirm the AP came up; record centre freq / width
```

If hostapd fails to start (`systemctl status hostapd`), the driver rejected the
capability — record `REJECTED` for that variant, restore from `/tmp/hostapd.conf.bak`,
and move on.

With the client associated and idle otherwise:

```bash
# TCP uplink (client -> Pi), three runs
for i in 1 2 3; do iperf3 -c <client-ip> -t 60; done
# TCP downlink (Pi -> client)
for i in 1 2 3; do iperf3 -c <client-ip> -t 60 -R; done
# UDP ceiling check
iperf3 -c <client-ip> -t 30 -u -b 100M
```

Record median of the three TCP runs per direction plus the UDP loss rate.

## 3. Results table (fill in)

| Variant | assoc OK | TCP ↓ Mbit/s | TCP ↑ Mbit/s | UDP loss % | Notes |
|---|---|---|---|---|---|
| A HT40 (ch6) | | | | | |
| B HT40+ (ch6) | | | | | |
| C HT40− (ch6) | | | | | |
| D HT20 (ch6) | | | | | |

Also note `iw dev uap0 station dump` tx bitrate per variant while pinging.

## 4. Decision rule

- Adopt a variant only if it beats the current config by **≥10% sustained median**
  in *both* directions **and** no client exhibits reconnect loops during the window.
- If B and C are rejected by the driver or within noise of A → close #3 as
  *"measured, current config optimal"*, paste the table into IMPROVEMENTS.md,
  and mark the item done with hardware evidence.
- If a variant wins: change `config/hostapd.conf` in-repo, deploy via
  `update-router.sh`, and note the measured delta.

## 5. Restore

```bash
sudo cp /tmp/hostapd.conf.bak /etc/hostapd/hostapd.conf
sudo systemctl restart hostapd
```
