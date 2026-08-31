#!/usr/bin/env bash
# Copy the local sunnyconf/ folder onto the device (rsync over ssh). No git push, no branch switch —
# fully reversible (device.sh op stays untouched; remove with: dsh "rm -rf $OP_DIR/sunnyconf").
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

say "deploy sunnyconf/  ->  $SUNNYCONF_HOST:$OP_DIR/sunnyconf/"
rsync -az --delete --exclude='__pycache__' --exclude='*.pyc' --exclude='scripts/' \
  -e "ssh ${SSH_OPTS[*]}" \
  "$REPO_ROOT/sunnyconf/" "$SUNNYCONF_HOST:$OP_DIR/sunnyconf/"

# sanity: does the device tree have the SDUI source the daemon reads?
dsh "test -f $OP_DIR/sunnypilot/sunnylink/settings_ui.json && echo 'settings_ui.json: present (full schema)' || echo 'settings_ui.json: MISSING (registry-only schema)'"
say "done. next:  bash sunnyconf/scripts/daemon.sh start"
