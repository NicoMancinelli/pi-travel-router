#!/usr/bin/env python3
"""
Captive portal login helper — tries YAML templates then generic POST.
Usage: python3 portal_login.py <ssid> <portal_url>
Exit 0 = login succeeded (internet reachable after attempt)
Exit 1 = login failed or not needed
"""
import sys
import os
import re
import glob
import subprocess

try:
    import urllib.request
    import urllib.parse
except ImportError:
    sys.exit(1)

PORTAL_DIR = "/etc/travel-router/portals"
SYSTEM_PORTAL_DIR = "/opt/pi-travel-router/config/portals"
PROBE_URL = "http://connectivitycheck.gstatic.com/generate_204"
COOKIE_JAR = "/tmp/portal-cookies.txt"

def internet_reachable():
    try:
        result = subprocess.run(
            ["curl", "-sf", "--max-time", "5", "-o", "/dev/null",
             "-w", "%{http_code}", PROBE_URL],
            capture_output=True, text=True, timeout=8
        )
        return result.stdout.strip() == "204"
    except Exception:
        return False

def load_templates(ssid):
    """Load portal templates matching the SSID, sorted by priority desc."""
    templates = []
    for d in [PORTAL_DIR, SYSTEM_PORTAL_DIR]:
        for path in glob.glob(os.path.join(d, "*.yaml")):
            try:
                # Simple YAML parser (no PyYAML dependency)
                tmpl = {"_path": path, "priority": 0, "fields": {}}
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if ":" in line and not line.startswith(" "):
                            k, _, v = line.partition(":")
                            tmpl[k.strip()] = v.strip().strip('"')
                        elif line.startswith("  ") and ":" in line:
                            k, _, v = line.partition(":")
                            tmpl["fields"][k.strip()] = v.strip().strip('"')
                pattern = tmpl.get("ssid_pattern", "*")
                if pattern == "*" or re.match(
                    pattern.replace("*", ".*"), ssid, re.IGNORECASE
                ):
                    try:
                        tmpl["priority"] = int(tmpl.get("priority", 0))
                    except ValueError:
                        tmpl["priority"] = 0
                    templates.append(tmpl)
            except Exception:
                continue
    return sorted(templates, key=lambda t: t["priority"], reverse=True)

def try_login(portal_url, template):
    """Attempt portal login using a template."""
    fields = template.get("fields", {})
    method = template.get("method", "POST").upper()
    login_url = portal_url
    # Override URL if template specifies a pattern
    url_pattern = template.get("login_url_pattern", "")
    if url_pattern and re.search(url_pattern, portal_url, re.IGNORECASE):
        login_url = portal_url  # use detected URL

    if not fields:
        # GET with no fields — just follow the redirect
        cmd = ["curl", "-sf", "--max-time", "10", "-L",
               "-c", COOKIE_JAR, "-b", COOKIE_JAR,
               "-A", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
               "-o", "/dev/null", login_url]
    elif method == "POST":
        data = "&".join(f"{urllib.parse.quote(k)}={urllib.parse.quote(v)}"
                        for k, v in fields.items())
        cmd = ["curl", "-sf", "--max-time", "10", "-L",
               "-c", COOKIE_JAR, "-b", COOKIE_JAR,
               "-A", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
               "-X", "POST", "-d", data,
               "-o", "/dev/null", login_url]
    else:  # GET with params
        sep = "&" if "?" in login_url else "?"
        params = "&".join(f"{urllib.parse.quote(k)}={urllib.parse.quote(v)}"
                          for k, v in fields.items())
        full_url = f"{login_url}{sep}{params}" if params else login_url
        cmd = ["curl", "-sf", "--max-time", "10", "-L",
               "-c", COOKIE_JAR, "-b", COOKIE_JAR,
               "-A", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
               "-o", "/dev/null", full_url]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        return result.returncode == 0
    except Exception:
        return False

def main():
    if len(sys.argv) < 3:
        print("Usage: portal_login.py <ssid> <portal_url>", file=sys.stderr)
        sys.exit(1)
    ssid = sys.argv[1]
    portal_url = sys.argv[2]

    templates = load_templates(ssid)
    for tmpl in templates:
        try_login(portal_url, tmpl)
        if internet_reachable():
            sys.exit(0)
    # Nothing worked
    sys.exit(1)

if __name__ == "__main__":
    main()
