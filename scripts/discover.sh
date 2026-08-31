#!/usr/bin/env bash
# "Do we see the comma over broadcast?" — browse for the daemon's mDNS advert.
#   discover.sh [secs]      browse from THIS machine (needs same L2 subnet as the comma)
#   discover.sh device      cross-check from the comma itself via its avahi (proves the advert is on-wire)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
HERE="$(dirname "${BASH_SOURCE[0]}")"

if [ "${1:-}" = "device" ]; then
  say "avahi-browse on the device ($SUNNYCONF_HOST):"
  dsh "avahi-browse -rpt _sunnyconf._tcp 2>/dev/null | grep -E '^=' || echo 'avahi-browse: nothing (or avahi-utils absent)'"
else
  say "mDNS browse from $(hostname) for _sunnyconf._tcp"
  say "(WSL2 doesn't receive LAN multicast reliably — if this finds nothing, use 'discover.sh device' or a phone)"
  python3 "$HERE/discover.py" "${1:-5}"
fi
