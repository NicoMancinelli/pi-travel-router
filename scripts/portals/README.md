# Per-SSID Captive Portal Scripts

## What are they?

When the travel router detects a captive portal, `captive-check.sh` first tries
to authenticate automatically using a generic POST of common accept-terms fields.
For hotel or venue networks that require specific field names, a redirect URL
format, or credentials, you can drop a **per-SSID script** that handles login
for that specific network.

## How it works

`captive-check.sh` derives a slug from the current SSID by replacing every
space and `/` with `_`, then looks for:

```
/etc/travel-router/portals/<slug>.sh
```

If that file exists and is executable, it is called with the portal's redirect
URL as `$1` **before** the generic fallback is attempted.  If the script returns
`0`, the generic attempt is skipped.  If it returns non-zero, the generic
attempt runs next.

### SSID slug derivation

The same transform used in `captive-check.sh`:

```bash
ssid_slug=$(printf '%s' "$current_ssid" | tr ' /' '__')
```

Examples:

| SSID | Filename |
|------|---------|
| `HotelGuest` | `HotelGuest.sh` |
| `Hotel Guest WiFi` | `Hotel_Guest_WiFi.sh` |
| `corp/guest` | `corp_guest.sh` |

## Script interface

- `$1` — the redirect URL captured from the portal probe (may be empty if the
  portal intercepts without a redirect; probe the redirect URL defensively)
- Return `0` to signal success (internet is expected to be clear)
- Return `1` (or any non-zero) to signal failure (generic login will be tried)

## Installing a script

```bash
# Copy your script into place
sudo cp MyHotel.sh /etc/travel-router/portals/MyHotel.sh

# Make it executable — this is the gate that captive-check.sh checks
sudo chmod +x /etc/travel-router/portals/MyHotel.sh
```

The portals directory is created by the installer.  Example scripts are
installed to `/etc/travel-router/portals/examples/` for reference — they are
not auto-loaded.

## Common curl patterns

```bash
# GET the portal page and save cookies
portal_html=$(curl -s --max-time 10 --interface wlan0 \
    -L -c /tmp/portal-cookies.txt "$1")

# Extract a form action
form_action=$(printf '%s' "$portal_html" | grep -oi 'action="[^"]*"' | head -1 | cut -d'"' -f2)

# POST form fields
curl -s -o /dev/null --max-time 10 --interface wlan0 \
    -b /tmp/portal-cookies.txt -c /tmp/portal-cookies.txt \
    -X POST "$form_action" \
    -d "field1=value1&field2=value2"

# Verify internet is clear after login
verify=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    --interface wlan0 "http://connectivitycheck.gstatic.com/generate_204")
[ "$verify" = "204" ] && return 0 || return 1
```

## Finding the right field names

The easiest way is to open the portal in a browser with DevTools open:

1. Open **DevTools** → **Network** tab → enable **Preserve log**
2. Fill in the form and click the accept/login button
3. Find the POST request in the Network tab and inspect its **Form Data** (or
   **Payload**) section — those are the exact field names and values to replicate
   in `curl -d`

## See also

- `example-accept-terms.sh` — template for click-through portals
- `example-credentials.sh` — template for portals that require username/password
