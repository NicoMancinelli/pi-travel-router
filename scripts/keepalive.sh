#!/bin/bash
# Pings Google every minute (via root cron) to keep iOS USB tether alive.
# Without this, iOS suspends the tether after ~60s of idle traffic.
ping -c 1 8.8.8.8 > /dev/null 2>&1
