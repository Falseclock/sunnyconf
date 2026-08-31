"""
sunnyconf — schema-driven field configuration for sunnypilot.

Folder map:
  daemon/   on-device Python service (HTTP + mDNS over WiFi) — runs as the managed process
            sunnyconf.daemon.main. All the runtime code, its config/ and tests/ live here.
  android/  native Android client (a dumb schema-driven renderer) — sources go here.
  docs/     ARCHITECTURE.md (daemon), INVESTIGATION.md (how sunnypilot stores settings),
            ANDROID.md (client design + contract).

See README.md for the overview.
"""
