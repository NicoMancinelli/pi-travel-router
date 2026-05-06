---
name: Feature request
about: Suggest a new capability or improvement
title: ''
labels: enhancement
assignees: ''
---

## Use case / problem being solved

<!-- What are you trying to do that you can't do today? A clear description of the gap, grounded in a real scenario. -->

---

## Proposed solution

<!-- Describe what you'd like the project to do. Be as concrete as you can — ideally sketch the config flag name, the command, or the behaviour change. -->

---

## Alternatives considered

<!-- Have you tried a workaround? Are there other ways to solve this? -->

---

## Hardware constraints to keep in mind

The Pi Zero 2 W is a constrained device:

- **CPU:** quad-core Cortex-A53 @ 1 GHz
- **RAM:** 512 MB (shared with GPU)
- **Storage:** microSD (slow random I/O)
- **USB:** one Micro-USB port, shared between power delivery (PWR) and USB gadget/OTG (USB)
- **Networking:** single 2.4 GHz 802.11n radio — AP/STA concurrent mode uses `iw dev wlan0 interface add uap0 type __ap` (a virtual interface on the same brcmfmac radio — NOT mac80211_hwsim); both halves share the same channel and throughput budget

Proposals that require a second USB port, significant RAM headroom (>50 MB sustained), or a 5 GHz radio should note the dependency explicitly.

---

## Additional context

<!-- Screenshots, links to similar projects, relevant config flag names (`ENABLE_*`), etc. -->
