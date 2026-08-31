#!/usr/bin/env bash
# Smoke-test the daemon API on the device (curl on the device's localhost, over ssh).
# usage: smoke.sh            # read-only checks
#        smoke.sh write      # also does one offroad-safe PUT round-trip (DynamicExperimentalControl)
#
# Auth: everything except /health needs a bearer token. This script pairs itself first by reading the pairing
# code the user set on the device (the SunnyconfPairingCode param, set in Settings -> Device) and POSTing /pair,
# then reuses the returned token. A real client asks the user to type that same code shown on the comma screen.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
B="http://127.0.0.1:$PORT"

say "GET /health (open)"; dsh "curl -s --max-time 5 $B/health"; echo

say "pair: read SunnyconfPairingCode from device + POST /pair"
TOKEN=$(dsh "
  CODE=\$(cd $OP_DIR && PYTHONPATH=$OP_DIR $REMOTE_PY -c 'from openpilot.common.params import Params; print(Params().get(\"SunnyconfPairingCode\") or \"\")')
  [ -n \"\$CODE\" ] || { echo 'NO_CODE — set a Pairing Code in Settings -> Device' >&2; exit 1; }
  curl -s --max-time 5 -X POST $B/pair -H 'Content-Type: application/json' \
    -d '{\"pin\":\"'\"\$CODE\"'\",\"client_id\":\"smoke\",\"label\":\"smoke.sh\"}' \
  | $REMOTE_PY -c 'import sys,json; print(json.load(sys.stdin).get(\"token\",\"\"))'
")
[ -n "$TOKEN" ] || { say "pairing failed (no pairing code set on device? daemon down?)"; exit 1; }
say "token acquired: ${TOKEN:0:8}…"
AUTH="-H \"Authorization: Bearer $TOKEN\""

say "GET /status";  dsh "curl -s --max-time 5 $AUTH $B/status"; echo
say "GET /schema (group -> count)"
dsh "curl -s --max-time 5 $AUTH $B/schema | $REMOTE_PY -c 'import sys,json; d=json.load(sys.stdin); print([(g[\"id\"],len(g[\"params\"])) for g in d[\"groups\"]]); print(\"total\", sum(len(g[\"params\"]) for g in d[\"groups\"]))'"
say "GET /params/Mads"; dsh "curl -s --max-time 5 $AUTH $B/params/Mads"; echo
say "401 check: GET /status with NO token — expect 401"
dsh "curl -s --max-time 5 -o /dev/null -w '%{http_code}\n' $B/status"

if [ "${1:-}" = "write" ]; then
  K=DynamicExperimentalControl
  say "PUT /params/$K = true (offroad-safe), then read back"
  dsh "curl -s --max-time 5 $AUTH -X PUT $B/params/$K -H 'Content-Type: application/json' -d '{\"value\":\"true\"}'"; echo
  dsh "curl -s --max-time 5 $AUTH $B/params/$K | $REMOTE_PY -c 'import sys,json; print(\"value now:\", json.load(sys.stdin)[\"value\"])'"
  say "409 check: PUT an offroad-only key (Mads) — expect offroad_required only if onroad"
  dsh "curl -s --max-time 5 $AUTH -o /dev/null -w '%{http_code}\n' -X PUT $B/params/Mads -H 'Content-Type: application/json' -d '{\"value\":\"true\"}'"
fi
