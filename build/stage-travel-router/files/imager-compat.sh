#!/bin/bash
# imager-compat.sh — extract Raspberry Pi Imager settings and neutralise firstrun.sh
set -euo pipefail

STATE_DIR="${STATE_DIR:-/var/lib/travel-router}"
mkdir -p "$STATE_DIR"
chmod 0700 "$STATE_DIR" 2>/dev/null || true

BOOT_DIR="${BOOT_DIR:-}"
if [ -z "$BOOT_DIR" ]; then
    for candidate in /boot/firmware /boot; do
        if [ -d "$candidate" ]; then
            BOOT_DIR="$candidate"
            break
        fi
    done
fi

FIRSTRUN=""
CMDLINE=""
if [ -n "$BOOT_DIR" ]; then
    [ -f "${BOOT_DIR}/firstrun.sh" ] && FIRSTRUN="${BOOT_DIR}/firstrun.sh"
    [ -f "${BOOT_DIR}/cmdline.txt" ] && CMDLINE="${BOOT_DIR}/cmdline.txt"
fi

# Fallback path checks
if [ -z "$FIRSTRUN" ]; then
    for candidate in /boot/firmware/firstrun.sh /boot/firstrun.sh; do
        if [ -f "$candidate" ]; then
            FIRSTRUN="$candidate"
            break
        fi
    done
fi
if [ -z "$CMDLINE" ]; then
    for cl in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
        if [ -f "$cl" ]; then
            CMDLINE="$cl"
            break
        fi
    done
fi

# Back up original firstrun.sh before any modifications if present
if [ -n "$FIRSTRUN" ] && [ -f "$FIRSTRUN" ]; then
    cp -f "$FIRSTRUN" "${STATE_DIR}/firstrun.sh.orig" 2>/dev/null || true
    chmod 0600 "${STATE_DIR}/firstrun.sh.orig" 2>/dev/null || true
fi

# Use Python to parse all Imager and boot drop-in settings robustly
if command -v python3 >/dev/null 2>&1; then
    python3 - << 'PYEOF'
import base64
import json
import os
import re
import shlex
import shutil
import subprocess

state_dir = os.environ.get("STATE_DIR", "/var/lib/travel-router")
os.makedirs(state_dir, exist_ok=True)
os.chmod(state_dir, 0o700)

boot_dirs = [os.environ["BOOT_DIR"]] if "BOOT_DIR" in os.environ and os.path.isdir(os.environ["BOOT_DIR"]) else ["/boot/firmware", "/boot"]
preseed = {}
root_password_hash = None
root_password_plain = None

def find_file(names):
    for bd in boot_dirs:
        for name in names:
            p = os.path.join(bd, name)
            if os.path.isfile(p):
                return p
    return None

firstrun_path = find_file(["firstrun.sh"])
if not firstrun_path and os.path.isfile(os.path.join(state_dir, "firstrun.sh.orig")):
    firstrun_path = os.path.join(state_dir, "firstrun.sh.orig")

userconf_path = find_file(["userconf.txt", "userconf"])
wpa_path = find_file(["wpa_supplicant.conf"])
env_dropin_path = find_file(["travel-router.env", "travel-router.conf", "firstboot.env"])

# 1. Parse drop-in configuration file (travel-router.env / travel-router.conf)
if env_dropin_path:
    try:
        shutil.copyfile(env_dropin_path, os.path.join(state_dir, "travel-router.env.orig"))
        os.chmod(os.path.join(state_dir, "travel-router.env.orig"), 0o600)
    except OSError:
        pass
    try:
        with open(env_dropin_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k:
                        preseed[k] = v
    except Exception as e:
        print(f"[imager-compat] Error reading dropin env {env_dropin_path}: {e}")

# 2. Parse userconf.txt (username:password_hash or username:plaintext)
if userconf_path:
    try:
        with open(userconf_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read().strip()
            if content and ":" in content:
                u, p = content.split(":", 1)
                u = u.strip()
                p = p.strip()
                if p.startswith("$"):
                    root_password_hash = p
                elif p:
                    root_password_plain = p
    except Exception as e:
        print(f"[imager-compat] Error reading userconf {userconf_path}: {e}")

# 3. Parse wpa_supplicant.conf
if wpa_path:
    try:
        with open(wpa_path, "r", encoding="utf-8", errors="replace") as fh:
            wpa_content = fh.read()
            m_cc = re.search(r'country=([A-Za-z]{2})', wpa_content, re.IGNORECASE)
            if m_cc and "COUNTRY" not in preseed:
                preseed["COUNTRY"] = m_cc.group(1).upper()
            m_ssid = re.search(r'ssid="([^"]+)"', wpa_content)
            if m_ssid and "AP_SSID" not in preseed:
                preseed["AP_SSID"] = m_ssid.group(1)
            m_psk = re.search(r'psk="([^"]+)"', wpa_content)
            if m_psk and "AP_PASS" not in preseed and len(m_psk.group(1)) >= 8:
                preseed["AP_PASS"] = m_psk.group(1)
    except Exception as e:
        print(f"[imager-compat] Error reading wpa_supplicant {wpa_path}: {e}")

# 4. Parse firstrun.sh
if firstrun_path:
    try:
        with open(firstrun_path, "r", encoding="utf-8", errors="replace") as fh:
            firstrun_content = fh.read()

        for line in firstrun_content.splitlines():
            line_str = line.strip()
            # OpenSSH public key
            if "SSH_ADMIN_KEY" not in preseed:
                m_key = re.search(
                    r'(ssh-(?:rsa|ed25519|dss|xmss)|ecdsa-sha2-[A-Za-z0-9]+|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-[A-Za-z0-9]+@openssh\.com)\s+([A-Za-z0-9+/]+=*)(\s+\S+)?',
                    line_str,
                )
                if m_key:
                    ktype = m_key.group(1)
                    kb = m_key.group(2)
                    kc = (m_key.group(3) or "").strip().rstrip("\"'")
                    pub = f"{ktype} {kb}" + (f" {kc}" if kc else "")
                    preseed["SSH_ADMIN_KEY"] = pub.strip()

            # Hostname
            if "ROUTER_HOSTNAME" not in preseed:
                m_h = re.search(r'raspi-config\s+nonint\s+do_hostname\s+(\S+)', line_str)
                if not m_h:
                    m_h = re.search(r'echo\s+(\S+)\s*>\s*/etc/hostname', line_str)
                if not m_h:
                    m_h = re.search(r'\b(?:ROUTER_)?HOSTNAME=(["\']?)([^"\'\s]+)\1', line_str)
                if m_h:
                    h_val = m_h.group(1 if len(m_h.groups()) == 1 else 2).strip("\"'")
                    if h_val:
                        preseed["ROUTER_HOSTNAME"] = h_val

            # Timezone
            if "ROUTER_TIMEZONE" not in preseed:
                m_tz = re.search(r'raspi-config\s+nonint\s+do_change_timezone\s+(\S+)', line_str)
                if not m_tz:
                    m_tz = re.search(r'timedatectl\s+set-timezone\s+(\S+)', line_str)
                if not m_tz:
                    m_tz = re.search(r'ln\s+.*zoneinfo/(\S+)\s+/etc/localtime', line_str)
                if not m_tz:
                    m_tz = re.search(r'\b(?:ROUTER_)?TIMEZONE=(["\']?)([^"\'\s]+)\1', line_str)
                if m_tz:
                    tz_val = m_tz.group(1 if len(m_tz.groups()) == 1 else 2).strip("\"'")
                    if tz_val:
                        preseed["ROUTER_TIMEZONE"] = tz_val

            # WiFi Country
            if "COUNTRY" not in preseed:
                m_cc = re.search(r'raspi-config\s+nonint\s+do_wifi_country\s+([A-Za-z]{2})', line_str, re.IGNORECASE)
                if not m_cc:
                    m_cc = re.search(r'\bCOUNTRY=(["\']?)([A-Za-z]{2})\1', line_str, re.IGNORECASE)
                if m_cc:
                    preseed["COUNTRY"] = m_cc.group(1 if len(m_cc.groups()) == 1 else 2).upper()

            # WiFi SSID & Passphrase
            if "AP_SSID" not in preseed:
                m_s = re.search(r'nmcli.*(?:ssid|\bconnect)\s+"([^"]+)"', line_str)
                if not m_s:
                    m_s = re.search(r'nmcli.*(?:ssid|\bconnect)\s+([^\s"\'=]+)', line_str)
                if not m_s:
                    m_s = re.search(r'\b(?:AP_)?SSID=(["\']?)([^"\'\n]+)\1', line_str)
                if m_s:
                    preseed["AP_SSID"] = m_s.group(1 if len(m_s.groups()) == 1 else 2).strip("\"'")

            if "AP_PASS" not in preseed:
                m_p = re.search(r'nmcli.*\bpassword\b\s+"([^"]+)"', line_str)
                if not m_p:
                    m_p = re.search(r'nmcli.*\bpassword\b\s+([^\s"\'=]+)', line_str)
                if not m_p:
                    m_p = re.search(r'wpa_passphrase\s+\S+\s+"?([^"\n]+)"?', line_str)
                if not m_p:
                    m_p = re.search(r'\b(?:AP_)?PASS=(["\']?)([^"\'\s]+)\1', line_str)
                if not m_p:
                    m_p = re.search(r'\b(?:WIFI_)?PASSWORD=(["\']?)([^"\'\s]+)\1', line_str)
                if m_p:
                    p_val = m_p.group(1 if len(m_p.groups()) == 1 else 2).strip("\"'")
                    if len(p_val) >= 8:
                        preseed["AP_PASS"] = p_val

            # User password from chpasswd in firstrun.sh
            if not root_password_hash and not root_password_plain:
                m_cp_e = re.search(r'echo\s+[\'"]?([^:\'"]+):([^\'"]+)[\'"]?\s*\|\s*chpasswd\s+-e', line_str)
                if m_cp_e:
                    root_password_hash = m_cp_e.group(2).strip()
                else:
                    m_cp = re.search(r'echo\s+[\'"]?([^:\'"]+):([^\'"]+)[\'"]?\s*\|\s*chpasswd', line_str)
                    if m_cp:
                        root_password_plain = m_cp.group(2).strip()

            # Generic variable preseed lines in firstrun.sh (e.g. TS_KEY="...", ENABLE_*=1)
            m_var = re.search(r'^(?:export\s+)?([A-Za-z0-9_]+)=(["\']?)(.*)\2$', line_str)
            if m_var:
                var_name = m_var.group(1)
                var_val = m_var.group(3).strip()
                if var_name not in preseed and var_val:
                    preseed[var_name] = var_val

    except Exception as e:
        print(f"[imager-compat] Error parsing firstrun {firstrun_path}: {e}")

# Apply SSH Public Key to /root/.ssh/authorized_keys
ssh_key = preseed.get("SSH_ADMIN_KEY")
if ssh_key:
    try:
        ssh_dir = os.environ.get("SSH_DIR", "/root/.ssh")
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        os.chmod(ssh_dir, 0o700)
        ak_path = os.path.join(ssh_dir, "authorized_keys")
        existing = ""
        if os.path.isfile(ak_path):
            with open(ak_path, "r", encoding="utf-8", errors="replace") as f:
                existing = f.read()
        if ssh_key.strip() not in [ln.strip() for ln in existing.splitlines()]:
            fd = os.open(ak_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (ssh_key.strip() + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        os.chmod(ak_path, 0o600)
    except Exception as e:
        print(f"[imager-compat] Error writing SSH key: {e}")

# Apply Root Password if detected
password_applied = False
if root_password_hash:
    try:
        if os.geteuid() == 0:
            subprocess.run(["chpasswd", "-e"], input=f"root:{root_password_hash}\n", text=True, check=True)
        password_applied = True
    except Exception as e:
        print(f"[imager-compat] Error setting root password hash: {e}")
elif root_password_plain:
    try:
        if os.geteuid() == 0:
            subprocess.run(["chpasswd"], input=f"root:{root_password_plain}\n", text=True, check=True)
        password_applied = True
        rpw_file = os.path.join(state_dir, "firstboot-rootpw")
        with open(rpw_file + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(f"root:{root_password_plain}\n")
        os.chmod(rpw_file + ".tmp", 0o600)
        os.replace(rpw_file + ".tmp", rpw_file)
    except Exception as e:
        print(f"[imager-compat] Error setting root password: {e}")

# If password was applied, cleanup insecure boot partition credential files
if password_applied:
    for bd in boot_dirs:
        for fname in ("root-password.txt", "userconf.txt", "userconf"):
            fpath = os.path.join(bd, fname)
            if os.path.isfile(fpath):
                try:
                    subprocess.run(["shred", "-u", fpath], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if os.path.exists(fpath):
                        os.remove(fpath)
                except OSError:
                    pass

# If drop-in file had secrets on FAT partition, securely remove from boot directory
if env_dropin_path and any(k in preseed for k in ("AP_PASS", "TS_KEY", "TOR_AP_PASS")):
    try:
        subprocess.run(["shred", "-u", env_dropin_path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(env_dropin_path):
            os.remove(env_dropin_path)
    except OSError:
        pass

# Save extracted preseed cache
preseed_json = os.path.join(state_dir, "imager-preseed.json")
try:
    with open(preseed_json + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(preseed, fh, indent=2)
    os.chmod(preseed_json + ".tmp", 0o600)
    os.replace(preseed_json + ".tmp", preseed_json)
except Exception as e:
    print(f"[imager-compat] Error saving preseed json: {e}")

preseed_env = os.path.join(state_dir, "imager-preseed.env")
try:
    lines = ["#!/bin/bash", "# Pre-seeded from Raspberry Pi Imager / boot partition", ""]
    for k, v in sorted(preseed.items()):
        lines.append(f"export {k}={shlex.quote(str(v))}")
    lines.append("")
    with open(preseed_env + ".tmp", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.chmod(preseed_env + ".tmp", 0o600)
    os.replace(preseed_env + ".tmp", preseed_env)
except Exception as e:
    print(f"[imager-compat] Error saving preseed env: {e}")

PYEOF
fi

# Neutralise firstrun.sh and clean cmdline.txt
if [ -n "$FIRSTRUN" ] && [ -f "$FIRSTRUN" ]; then
    TMPSTUB=$(mktemp "${FIRSTRUN}.XXXXXX" 2>/dev/null || mktemp "/tmp/firstrun.XXXXXX")
    cat > "$TMPSTUB" << 'STUB'
#!/bin/bash
# Neutralised by pi-travel-router imager-compat: Settings migrated securely.
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
fi

if [ -n "$CMDLINE" ] && [ -f "$CMDLINE" ]; then
    sed -i 's| systemd\.run=[^ ]*||g; s| systemd\.run_success_action=[^ ]*||g; s| systemd\.unit=kernel-command-line\.target||g' "$CMDLINE" 2>/dev/null || true
fi

exit 0

