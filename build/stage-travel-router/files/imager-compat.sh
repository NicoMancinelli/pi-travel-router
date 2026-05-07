#!/bin/bash
# imager-compat.sh — neutralise Raspberry Pi Imager firstrun.sh
set -euo pipefail

FIRSTRUN=""
for candidate in /boot/firmware/firstrun.sh /boot/firstrun.sh; do
    if [ -f "$candidate" ]; then
        FIRSTRUN="$candidate"
        break
    fi
done

[ -z "$FIRSTRUN" ] && exit 0

# Fingerprint: the Imager mechanism works by adding systemd.run=<path> to
# cmdline.txt.  That entry is the canonical indicator that firstrun.sh was
# placed by Imager — not the content of firstrun.sh itself, which varies
# significantly across Imager versions (some omit raspi-config entirely and
# use nmcli; some don't set an SSH key at all).  Checking cmdline.txt is
# more robust than grepping firstrun.sh for raspi-config/authorized_keys.
CMDLINE=""
for cl in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [ -f "$cl" ]; then
        CMDLINE="$cl"
        break
    fi
done

if [ -z "$CMDLINE" ] || ! grep -qE 'systemd\.run=' "$CMDLINE" 2>/dev/null; then
    # No Imager cmdline marker — not an Imager firstrun.sh; leave it alone.
    exit 0
fi

# Extract SSH public key(s) and write to /root/.ssh/authorized_keys.
# Scan every line of firstrun.sh for a valid OpenSSH public-key token.
# Imager encodes the key in different ways across versions:
#   - echo 'ssh-ed25519 AAAA...' >> /root/.ssh/authorized_keys
#   - SSHPUBKEY="ssh-ed25519 AAAA..."
#   - install -m 0600 /dev/stdin /root/.ssh/authorized_keys <<< "ssh-rsa AAAA..."
# The regex below matches the key type + base64 blob + optional comment
# regardless of surrounding shell syntax.
PUBKEY=""
while IFS= read -r line; do
    KEY=$(printf '%s\n' "$line" | grep -oP '(ssh-(rsa|ed25519|dss|xmss)|ecdsa-sha2-[A-Za-z0-9]+) [A-Za-z0-9+/]+=*( \S+)?' | head -1)
    if [ -n "$KEY" ]; then
        PUBKEY="$KEY"
        break
    fi
done < "$FIRSTRUN"

if [ -n "$PUBKEY" ]; then
    mkdir -p /root/.ssh
    chmod 0700 /root/.ssh
    chown root:root /root/.ssh
    AK=/root/.ssh/authorized_keys
    # Create the file with correct permissions atomically: touch with explicit
    # mode so we never have a window where the file exists world-readable.
    if [ ! -f "$AK" ]; then
        install -m 0600 -o root -g root /dev/null "$AK"
    fi
    if ! grep -qF "$PUBKEY" "$AK" 2>/dev/null; then
        echo "$PUBKEY" >> "$AK"
    fi
    chmod 0600 "$AK"
    chown root:root "$AK"
fi

# Rewrite firstrun.sh to a minimal safe stub — only the cmdline.txt cleanup
# that the systemd.run= boot mechanism expects.
# Write to a temp file then rename for atomicity (avoids a corrupt stub if
# interrupted mid-write).
TMPSTUB=$(mktemp "${FIRSTRUN}.XXXXXX")
cat > "$TMPSTUB" << 'STUB'
#!/bin/bash
# Neutralised by pi-travel-router imager-compat: SSH key already applied to root.
# Remove systemd.run entries from cmdline.txt so this doesn't re-run.
if [ -f /boot/firmware/cmdline.txt ]; then
    sed -i 's| systemd\.run=[^ ]*||g; s| systemd\.run_success_action=[^ ]*||g; s| systemd\.unit=kernel-command-line\.target||g' /boot/firmware/cmdline.txt
fi
if [ -f /boot/cmdline.txt ]; then
    sed -i 's| systemd\.run=[^ ]*||g; s| systemd\.run_success_action=[^ ]*||g; s| systemd\.unit=kernel-command-line\.target||g' /boot/cmdline.txt
fi
STUB
chmod 0755 "$TMPSTUB"
mv "$TMPSTUB" "$FIRSTRUN"

exit 0
