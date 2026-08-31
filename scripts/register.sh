#!/usr/bin/env bash
# Register (or unregister) sunnyconf as a managed openpilot process on the device, so the manager
# auto-starts it on every boot/restart. Idempotent. Edits $OP_DIR/system/manager/process_config.py in place.
# The edit persists across reboot only if updates are disabled (device.sh updates-off).
#   register.sh            add the registration
#   register.sh remove     remove it
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MODE="${1:-add}"
say "$MODE sunnyconf in $OP_DIR/system/manager/process_config.py  ($SUNNYCONF_HOST)"
dsh "OP_DIR=$OP_DIR MODE=$MODE $REMOTE_PY -" <<'PY'
import os
pc = os.path.join(os.environ["OP_DIR"], "system/manager/process_config.py")
mode = os.environ["MODE"]
src = open(pc).read()
line = '  PythonProcess("sunnyconf", "sunnyconf.daemon.main", always_run, restart_if_crash=True),\n'

if mode == "remove":
    if line not in src:
        print("not registered; nothing to remove"); raise SystemExit(0)
    open(pc, "w").write(src.replace(line, ""))
    print("unregistered"); raise SystemExit(0)

if "sunnyconf.daemon.main" in src:
    print("already registered"); raise SystemExit(0)
anchor = 'NativeProcess("locationd_llk"'
i = src.find(anchor)
if i < 0:
    print("ANCHOR 'locationd_llk' NOT FOUND — process_config.py NOT modified"); raise SystemExit(1)
eol = src.find("\n", i) + 1
open(pc, "w").write(src[:eol] + line + src[eol:])
print("registered after locationd_llk")
PY
say "apply it now:  bash sunnyconf/scripts/daemon.sh stop && bash sunnyconf/scripts/device.sh op-restart"
say "or just reboot — the manager will launch sunnyconf automatically (survives reboot; DisableUpdates=1)"
