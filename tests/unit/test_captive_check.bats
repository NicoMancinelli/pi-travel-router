#!/usr/bin/env bats
# Unit tests for captive-check.sh form_action extraction logic.
# Tests the grep+cut pipeline used on line 58 of captive-check.sh:
#   form_action=$(printf '%s' "$portal_html" \
#       | grep -oi 'action="[^"]*"' | head -1 | cut -d'"' -f2)

setup() {
    # Helper: run the same pipeline used in captive-check.sh
    extract_action() {
        local html="$1"
        printf '%s' "$html" \
            | grep -oi 'action="[^"]*"' \
            | head -1 \
            | cut -d'"' -f2
    }
}

@test "form_action: extracts double-quoted action URL" {
    local html='<form method="POST" action="/login">'
    result=$(extract_action "$html")
    [ "$result" = "/login" ]
}

@test "form_action: extracts full https URL from action attribute" {
    local html='<form action="https://portal.example.com/auth" method="post">'
    result=$(extract_action "$html")
    [ "$result" = "https://portal.example.com/auth" ]
}

@test "form_action: returns empty string when no action attribute present" {
    local html='<form method="POST">'
    result=$(extract_action "$html")
    [ -z "$result" ]
}

@test "form_action: handles action attribute with path containing slash and equals" {
    local html='<FORM ACTION="/captive/login?redirect=http://example.com" METHOD="POST">'
    result=$(extract_action "$html")
    [ "$result" = "/captive/login?redirect=http://example.com" ]
}
