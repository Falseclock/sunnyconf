#!/usr/bin/env bash
# sunnyconf/scripts/lib.sh — shared config + helpers for on-device testing. Sourced by the other scripts.
#
# NOTHING here is bound to a specific device/IP/key — everything is an override (keep it universal).
# Point it at a device by setting an ssh alias `comma` in ~/.ssh/config, OR overriding per invocation:
#   SUNNYCONF_HOST=comma4 bash sunnyconf/scripts/smoke.sh
#
#   SUNNYCONF_HOST   ssh target: a ~/.ssh/config alias or user@ip   default: comma  (define this alias)
#   SUNNYCONF_KEY    ssh identity file (optional)                   default: (use ssh_config / agent)
#   OP_DIR           openpilot tree on the device                   default: /data/openpilot
#   PORT             daemon port                                    default: 8765
#   REMOTE_PY        python interpreter on the device               default: /usr/local/venv/bin/python3
set -uo pipefail

: "${SUNNYCONF_HOST:=comma}"
: "${SUNNYCONF_KEY:=}"
: "${OP_DIR:=/data/openpilot}"
: "${PORT:=8765}"

# openpilot's python venv is baked into AGNOS at /usr/local/venv (NOT inside the repo). The code lives at
# $OP_DIR and is put on PYTHONPATH; the interpreter + deps (zmq, params, ...) are this venv python.
# (openpilot also keeps /data/pythonpath as a symlink -> the active tree, created by launch_chffrplus.sh:70.)
: "${REMOTE_PY:=/usr/local/venv/bin/python3}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # .../openpilot

SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
[ -n "$SUNNYCONF_KEY" ] && SSH_OPTS+=(-o IdentitiesOnly=yes -i "$SUNNYCONF_KEY")

# run a shell command on the device
dsh()  { ssh "${SSH_OPTS[@]}" "$SUNNYCONF_HOST" "$@"; }
# run a command on the device with an interactive tty (for attach/shell)
dsht() { ssh -tt "${SSH_OPTS[@]}" "$SUNNYCONF_HOST" "$@"; }
# run the openpilot venv python on the device with the repo on PYTHONPATH
dpy()  { dsh "cd '$OP_DIR' && PYTHONPATH='$OP_DIR' '$REMOTE_PY' $*"; }

say() { printf '\033[1;36m» %s\033[0m\n' "$*" >&2; }
