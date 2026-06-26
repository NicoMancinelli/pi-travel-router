#!/bin/bash
# Rotate the combined travel-router log when it exceeds 5 MB.
# Invoked by travel-router-log-rotate.service via systemd timer.
set -euo pipefail

LOG_DIR="/var/log/travel-router"
LOG_FILE="${LOG_DIR}/combined.log"
MAX_BYTES=5242880  # 5 MiB

mkdir -p "$LOG_DIR"

if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt "$MAX_BYTES" ]; then
    mv "$LOG_FILE" "${LOG_FILE}.1"
    gzip -f "${LOG_FILE}.1"
fi
