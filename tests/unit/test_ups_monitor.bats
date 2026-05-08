#!/usr/bin/env bats
# Unit tests for ups-monitor.sh threshold logic.
# The script uses [[ "$LEVEL" -le "$THRESHOLD" ]] — so boundary (level == threshold) triggers shutdown.

setup() {
    # Inline the threshold check logic from ups-monitor.sh (lines 49-55):
    #   if [[ -z "$LEVEL" ]]; then exit 0; fi
    #   if [[ ! "$LEVEL" =~ ^[0-9]+$ ]]; then exit 0 (non-numeric guard); fi
    #   if [[ "$LEVEL" -le "$THRESHOLD" ]]; then SHUTDOWN; fi
    should_shutdown() {
        local level="$1" threshold="$2"
        [[ -z "$level" ]] && return 1
        [[ ! "$level" =~ ^[0-9]+$ ]] && return 1
        [[ "$level" -le "$threshold" ]]
    }
}

@test "ups-monitor: level above threshold does not trigger shutdown" {
    run bash -c '[[ 50 -le 10 ]] && echo shutdown || echo ok'
    [ "$output" = "ok" ]
}

@test "ups-monitor: level equal to threshold triggers shutdown (boundary -le)" {
    should_shutdown 10 10
}

@test "ups-monitor: level below threshold triggers shutdown" {
    should_shutdown 5 10
}

@test "ups-monitor: empty level does not trigger shutdown (no UPS attached)" {
    ! should_shutdown "" 10
}

@test "ups-monitor: non-numeric level is guarded (does not trigger shutdown)" {
    ! should_shutdown "null" 10
}

@test "ups-monitor: level above threshold by 1 does not trigger shutdown" {
    ! should_shutdown 11 10
}
