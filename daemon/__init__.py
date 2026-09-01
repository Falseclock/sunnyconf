"""
sunnyconf — in-fork, schema-driven configuration daemon for sunnypilot.

Serves the sunnypilot param settings over the local WiFi network (HTTP + mDNS) so a
native Android client can read and edit them in the field with no internet connection
and no APK rebuild when new toggles ship.

The settings schema is DERIVED at startup from the live source tree:
  * sunnypilot/sunnylink/settings_ui.json (SDUI)  — primary: groups, titles, widgets, options,
                                                    ranges, declarative offroad rules
  * common/params_keys.h via Params()             — authoritative type + default per key
  * config/schema_overrides.json                  — hand fixes + keys not yet in the SDUI

So a fork update that adds a setting to settings_ui.json shows up in the menu automatically,
without touching this daemon or the client.

See sunnyconf/README.md and docs/ARCHITECTURE.md for the full design and contract.
"""

SCHEMA_VERSION = 1
# The daemon's own release version, surfaced in /status as daemon_version. The app compares it against the
# minimum its features need and tells the user to update the submodule when the daemon is older.
# Semver: PATCH for fixes (so support can tell builds apart), MINOR for endpoint/contract additions
# (backup/restore + daemon_version shipped in 1.1.0; 1.1.1 = mDNS startup burst, install.py comma4 fix).
DAEMON_VERSION = "1.1.1"
DEFAULT_PORT = 8765
SERVICE_TYPE = "_sunnyconf._tcp.local."
SERVICE_INSTANCE = "comma"   # generic fallback only; the real instance name is derived per-device (device.py)

# Params that must never be modified through this daemon — they grant trust (whose keys may SSH in), fake
# consent, or ARE the pairing secret. Kept in sync with sunnypilot/sunnylink/athena/sunnylinkd.py:BLOCKED_PARAMS.
# NOTE this is deliberately NARROWER than the SDUI's `blocked` flag ("Device only" in the cloud): the cloud
# blocks SSH/ADB *toggles* because a sunnylink account works from anywhere on the internet; our client pairs
# with a PIN shown on the device's own screen and talks over the local WiFi, which is as good as standing at
# the device — so the toggles are writable here, and only the trust-granting params stay locked (see
# docs/ARCHITECTURE.md).
BLOCKED_PARAMS = frozenset({
  "CompletedSunnylinkConsentVersion", "CompletedTrainingVersion",
  "GithubUsername", "GithubSshKeys", "HasAcceptedTerms", "HasAcceptedTermsSP",
  "SunnyconfPairingCode",
})
