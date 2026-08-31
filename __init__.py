"""
sunnyconf — schema-driven field configuration for sunnypilot.

Folder map:
  daemon/   on-device Python service (HTTP + mDNS over WiFi) — runs as the managed process
            sunnyconf.daemon.main. All the runtime code, its config/ and tests/ live here.
  scripts/  dev helpers (deploy over ssh, smoke test, mDNS discovery).
  tools/    head-unit helpers (screen recording, fake ignition).
The Android client lives in its own repo: https://github.com/Falseclock/sunnyconf-app

See README.md for the overview.
"""
