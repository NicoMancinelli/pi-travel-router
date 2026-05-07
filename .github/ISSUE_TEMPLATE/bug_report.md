---
name: Bug report
about: Something isn't working as expected
title: ''
labels: bug
assignees: ''
---

## Environment

**Pi hardware**
<!-- Pi Zero 2 W assumed — note if you are running on different hardware -->

**OS on the Pi**
```
# Run on the Pi:
cat /etc/os-release
```

**Router version**
```
# Image installs:
cat /etc/travel-router-image-version 2>/dev/null || cat /opt/pi-travel-router/VERSION
```

**How you installed**
- [ ] Flashed the pre-built `.img.xz` and used the firstboot wizard
- [ ] Manual `install.sh` on a fresh Pi OS Lite Bookworm
- [ ] `update-router.sh` update on an existing install

---

## Steps to reproduce

1. 
2. 
3. 

---

## Expected behaviour

<!-- What should have happened? -->

---

## Actual behaviour

<!-- What happened instead? -->

---

## Relevant logs

Run `travel-diagnostic` on the Pi to collect a redacted snapshot of logs, network state, and config:

```sh
sudo travel-diagnostic
# Produces /tmp/travel-diagnostic-<timestamp>.tar.gz
# Copy to your laptop: scp root@192.168.7.1:/tmp/travel-diagnostic-*.tar.gz .
```

Attach the tar.gz, or paste the relevant excerpts below.

<details>
<summary>Log output</summary>

```
# paste here
```

</details>

---

## Additional context

<!-- Anything else that might help: uplink type (iPhone USB / Android USB / hotel WiFi / Bluetooth PAN), optional features enabled (ENABLE_DOT, ENABLE_ADGUARD, etc.), steps already tried. -->
