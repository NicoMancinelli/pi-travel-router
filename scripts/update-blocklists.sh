#!/bin/bash
# Download Firehol Level 1 blocklist and load into nftables set
# RAM-safe: uses Python to write the nft file rather than bash string ops
# Runs daily via update-blocklists.timer

set -euo pipefail

# shellcheck source=/dev/null
source /etc/default/travel-router 2>/dev/null || true

if [ "${ENABLE_BLOCKLISTS:-0}" != "1" ]; then
    echo "Blocklists disabled: set ENABLE_BLOCKLISTS=1 in /etc/default/travel-router"
    exit 0
fi

BLOCKLIST_URL="https://raw.githubusercontent.com/firehol/blocklist-ipsets/master/firehol_level1.netset"
TMP_FILE="/tmp/firehol_level1.netset"
NFT_FILE="/etc/nftables.d/blocklists.nft"
NFT_NEW="${NFT_FILE}.new"
MAX_BLOCKLIST_ENTRIES="${MAX_BLOCKLIST_ENTRIES:-20000}"

case "$MAX_BLOCKLIST_ENTRIES" in
    ''|*[!0-9]*)
        echo "MAX_BLOCKLIST_ENTRIES must be a positive integer"
        exit 1
        ;;
esac
if [ "$MAX_BLOCKLIST_ENTRIES" -lt 1 ]; then
    echo "MAX_BLOCKLIST_ENTRIES must be greater than 0"
    exit 1
fi

mkdir -p /etc/nftables.d

echo "Fetching Firehol Level 1 blocklist..."
curl -s --max-time 60 -o "$TMP_FILE" "$BLOCKLIST_URL" || {
    echo "Failed to fetch blocklist — keeping existing rules"
    exit 0
}

COUNT=$(grep -c -v '^#' "$TMP_FILE" 2>/dev/null || echo 0)
echo "Fetched $COUNT entries"

# Use Python for RAM-safe file generation (avoids bash tr/sed on large strings)
python3 - "$TMP_FILE" "$NFT_NEW" "$MAX_BLOCKLIST_ENTRIES" << 'PYEOF'
import sys, datetime

src, dst, max_entries = sys.argv[1], sys.argv[2], int(sys.argv[3])
entries = []
with open(src) as source:
    for line in source:
        entry = line.strip()
        if entry and not entry.startswith('#'):
            entries.append(entry)
            if len(entries) >= max_entries:
                break

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

nft -c -f "$NFT_NEW" || {
    echo "nft validation failed — keeping existing rules"
    rm -f "$NFT_NEW"
    exit 1
}

# H8: persist to disk FIRST so a reboot always loads the new file,
# then load from the now-persisted path.
mv "$NFT_NEW" "$NFT_FILE"
if nft -f "$NFT_FILE"; then
    echo "Blocklist loaded: $COUNT entries (max $MAX_BLOCKLIST_ENTRIES)"
else
    echo "nft load failed — file persisted to disk but in-kernel rules may be stale"
    exit 1
fi

if [ -n "${NTFY_TOPIC:-}" ]; then
    /usr/local/bin/notify-router.sh "Blocklist updated: up to $MAX_BLOCKLIST_ENTRIES IP ranges loaded" 2>/dev/null || true
fi
