#!/usr/bin/env bash
# Device management: persistence flags + openpilot control. See ../README.md section "Running & testing".
# usage: device.sh <cmd>
#   status         branch / tmux / service / offroad + DisableUpdates
#   updates-off    DisableUpdates=1 + stop updater  (on-device edits survive reboot)
#   updates-on     DisableUpdates=0                 (re-enable OTA)
#   quickboot-on   touch prebuilt   (skip build.py on boot)
#   quickboot-off  rm prebuilt      (rebuild on boot)
#   op-stop        stop openpilot (tmux kill-session comma / pkill manager.py)
#   op-start       sudo systemctl start comma
#   op-restart     sudo systemctl restart comma
#   op-attach      attach the openpilot tmux session (interactive terminal only)
#   shell          interactive shell on the device in $OP_DIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

pybool() { dsh "cd $OP_DIR && PYTHONPATH=$OP_DIR $REMOTE_PY -c 'from openpilot.common.params import Params; Params().put_bool(\"$1\", $2)'"; }

case "${1:-}" in
  status)
    dsh "cd $OP_DIR && echo -n 'branch: '; git rev-parse --abbrev-ref HEAD 2>/dev/null; git rev-parse --short HEAD 2>/dev/null; echo -n 'openpilot service: '; systemctl is-active comma.service 2>/dev/null; echo -n 'tmux: '; tmux ls 2>/dev/null; PYTHONPATH=$OP_DIR $REMOTE_PY -c 'from openpilot.common.params import Params as P; p=P(); print(\"IsOnroad=\",p.get_bool(\"IsOnroad\"),\"DisableUpdates=\",p.get_bool(\"DisableUpdates\"))'; echo -n 'prebuilt: '; test -f $OP_DIR/prebuilt && echo yes || echo no" ;;
  updates-off)
    pybool DisableUpdates True; dsh "sh $OP_DIR/scripts/stop_updater.sh 2>/dev/null || true"; say "DisableUpdates=1 — on-device edits now survive reboot/OTA" ;;
  updates-on)
    pybool DisableUpdates False; say "DisableUpdates=0 — OTA re-enabled" ;;
  quickboot-on)
    dsh "touch $OP_DIR/prebuilt"; say "prebuilt created — boot skips build.py" ;;
  quickboot-off)
    dsh "rm -f $OP_DIR/prebuilt"; say "prebuilt removed — boot rebuilds" ;;
  op-stop)
    dsh "tmux kill-session -t comma 2>/dev/null || pkill -f '[m]anager.py'; echo 'openpilot stopped'" ;;
  op-start)
    dsh "sudo systemctl start comma; echo 'openpilot start requested'" ;;
  op-restart)
    # WARNING: `systemctl restart comma` re-runs launch_openpilot.sh, which includes AGNOS's boot-time
    # hold-screen-to-reset check. If the touchscreen registers taps during the ~30s relaunch, it shows the
    # factory-RESET screen instead of booting openpilot (nothing is erased unless you confirm it). Do NOT
    # touch the screen during a restart. For iterating on daemon code, prefer the STANDALONE daemon
    # (sunnyconf/scripts/daemon.sh) which loads fresh disk code with no manager restart and no tap check.
    say "restarting openpilot — DO NOT touch the screen during boot (~30s) or AGNOS may show the reset prompt"
    dsh "sudo systemctl restart comma; echo 'openpilot restart requested'" ;;
  op-attach)
    dsht "tmux a -t comma" ;;
  shell)
    dsht "cd $OP_DIR && exec bash -l" ;;
  *)
    echo "usage: device.sh status|updates-off|updates-on|quickboot-on|quickboot-off|op-stop|op-start|op-restart|op-attach|shell"; exit 1 ;;
esac
