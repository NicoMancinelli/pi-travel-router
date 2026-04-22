#!/bin/bash
# Download Firehol Level 1 blocklist and load into nftables set
# RAM-safe: uses Python to write the nft file rather than bash string ops
# Runs daily via update-blocklists.timer

set -euo pipefail

BLOCKLIST_URL="https://raw.githubusercontent.com/firehol/blocklist-ipsets/master/firehol_level1.netset"
TMP_FILE="/tmp/firehol_level1.netset"
NFT_FILE="/etc/nftables.d/blocklists.nft"

mkdir -p /etc/nftables.d

echo "Fetching Firehol Level 1 blocklist..."
curl -s --max-time 60 -o "$TMP_FILE" "$BLOCKLIST_URL" || {
    echo "Failed to fetch blocklist — keeping existing rules"
    exit 0
}

COUNT=$(grep -c -v '^#' "$TMP_FILE" 2>/dev/null || echo 0)
echo "Fetched $COUNT entries"

# Use Python for RAM-safe file generation (avoids bash tr/sed on large strings)
python3 - "$TMP_FILE" "$NFT_FILE" << 'PYEOF'
import sys, datetime

src, dst = sys.argv[1], sys.argv[2]
entries = [l.strip() for l in open(src) if l.strip() and not l.startswith('#')]

with open(dst, 'w') as f:
    f.write("#!/usr/sbin/nft -f\n")
    f.write(f"# Firehol Level 1 — {datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n")
    f.write("table inet blocklists {\n")
    f.write("    set firehol_l1 {\n")
    f.write("        type ipv4_addr\n")
    f.write("        flags interval\n")
    f.write("        auto-merge\n")
    f.write("        elements = {\n")
    for i, entry in enumerate(entries):
        sep = "," if i < len(entries) - 1 else ""
        f.write(f"            {entry}{sep}\n")
    f.write("        }\n    }\n\n")
    f.write("    chain forward {\n")
    f.write("        type filter hook forward priority -1;\n")
    f.write("        ip saddr @firehol_l1 drop\n")
    f.write("        ip daddr @firehol_l1 drop\n")
    f.write("    }\n\n")
    f.write("    chain input {\n")
    f.write("        type filter hook input priority -1;\n")
    f.write("        ip saddr @firehol_l1 drop\n")
    f.write("    }\n}\n")

print(f"Written {len(entries)} entries to {dst}")
PYEOF

nft -f "$NFT_FILE" && echo "Blocklist loaded: $COUNT entries" || {
    echo "nft load failed — check $NFT_FILE"
    exit 1
}

source /etc/default/travel-router 2>/dev/null || true
if [ -n "${NTFY_TOPIC:-}" ]; then
    /usr/local/bin/notify-router.sh "Blocklist updated: $COUNT IPs blocked" 2>/dev/null || true
fi
