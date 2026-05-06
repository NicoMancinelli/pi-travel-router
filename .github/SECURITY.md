# Security Policy

## Supported versions

Only the **latest release** receives security fixes. Older versions are not patched.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Older releases | No |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Send an email to **nicomancinelli@gmail.com** with the subject line:

```
pi-travel-router security
```

Include in your report:

- **Description** — what the vulnerability is and which component is affected
- **Steps to reproduce** — a minimal, reliable reproduction
- **Impact** — what an attacker could achieve by exploiting it
- **Suggested fix** (optional but appreciated)

## Response commitment

- **Acknowledgement:** within 7 days of receipt
- **Status update:** within 14 days (confirmed, investigating, or not-a-bug)
- **Patch for critical issues:** within 30 days of confirmation

## Scope and context

This is a hobbyist open-source project, not a commercial product. The Pi Zero 2 W travel router is intended for personal use on a trusted LAN segment. The attack surface is:

- The Pi OS Bookworm base system
- The firstboot wizard (Python stdlib HTTP server, port 80, first-boot only)
- Shell scripts in `scripts/` and `install.sh`
- The nftables/iptables-nft firewall ruleset
- The Tailscale subnet router configuration

The image ships with a **default root password (`changeme`)** that must be changed on first boot. This is intentional for accessibility and is documented prominently. Reports about this default alone are acknowledged but not treated as vulnerabilities.

Thank you for helping keep this project safe for everyone who uses it.
