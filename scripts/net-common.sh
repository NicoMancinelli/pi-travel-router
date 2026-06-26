#!/bin/bash
# Shared network constants for travel-router watchdog scripts.
# Source this file; do not execute directly.

# Canonical connectivity probe URLs used by failover-watchdog and wan-watchdog.
# Changing them here updates both scripts simultaneously.
_TR_PROBE_URL_204="http://connectivitycheck.gstatic.com/generate_204"
_TR_PROBE_URL_DETECT="https://detectportal.firefox.com/success.txt"
