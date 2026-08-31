"""
sunnyconf.main — daemon entrypoint, launched as a managed process by the openpilot manager
(system/manager/process_config.py: PythonProcess("sunnyconf", "sunnyconf.daemon.main", always_run)).

The manager calls main() directly, so it must exist at module top level. We:
  1. build the schema from the live tree (primarily the SDUI settings_ui.json),
  2. announce over mDNS,
  3. serve HTTP (blocking),
  4. watch GitCommit so a fork update (new settings) is re-detected without a manual refresh.
"""
from __future__ import annotations

import signal
import threading

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from . import DEFAULT_PORT
from . import auth, device, discovery, drives, schema_gen, server


def _log_pairing_state(params):
  """Log whether pairing is enabled — WITHOUT logging the secret. The pairing code is the
  SunnyconfPairingCode param, set by the user on the device (Settings -> Device -> Pairing code).
  Already-paired clients keep their token regardless; the code is only needed to pair a NEW device."""
  code = params.get("SunnyconfPairingCode")
  if auth.code_set(code):
    cloudlog.info("sunnyconf: pairing ENABLED (SunnyconfPairingCode is set)")
  else:
    cloudlog.warning("sunnyconf: pairing DISABLED — set a code in Settings -> Device -> Pairing code")


def _commit_watch(stop: threading.Event):
  """Rebuild the schema when the running code changes (after an OTA the daemon usually restarts, but this
  also covers a live git pull), so new settings appear without a manual ?refresh."""
  last = schema_gen.build_schema()["sunnypilot"]["commit"]
  while not stop.wait(15):
    try:
      schema_gen.invalidate()
      commit = schema_gen.build_schema()["sunnypilot"]["commit"]
      if commit != last:
        cloudlog.info(f"sunnyconf: commit changed {last} -> {commit}, rebuilt schema")
        last = commit
    except Exception:
      cloudlog.exception("sunnyconf: commit watch tick failed")


def main():
  cloudlog.info("sunnyconf: starting")
  params = Params()
  schema_gen.build_schema(refresh=True)
  _log_pairing_state(params)

  announcer = discovery.start_discovery(DEFAULT_PORT, device.mdns_txt(params), device.instance_name(params))

  httpd = server.make_server(DEFAULT_PORT)
  stop = threading.Event()

  def _shutdown(*_):
    stop.set()
    threading.Thread(target=httpd.shutdown, daemon=True).start()
  signal.signal(signal.SIGTERM, _shutdown)
  signal.signal(signal.SIGINT, _shutdown)

  watcher = threading.Thread(target=_commit_watch, args=(stop,), name="sunnyconf_watch", daemon=True)
  watcher.start()

  # drives watcher: index each recording segment as soon as it finishes being written, so the Drives
  # screen is always current (throttled + capped while onroad; see drives._watch_loop)
  drives.ensure_indexing()

  cloudlog.info(f"sunnyconf: serving on 0.0.0.0:{DEFAULT_PORT}")
  try:
    httpd.serve_forever()
  finally:
    stop.set()
    if announcer:
      announcer.stop()
    try:
      httpd.server_close()
    except Exception:
      pass
    cloudlog.info("sunnyconf: stopped")


if __name__ == "__main__":
  main()
