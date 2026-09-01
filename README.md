# sunnyconf

Schema-driven **local configuration** for [sunnypilot](https://github.com/sunnypilot/sunnypilot): edit the
car's params over WiFi from a native Android app — no internet, no comma/sunnylink account, and no APK
rebuild when new toggles ship. The settings schema is derived **live on the device** (primarily from the
sunnylink SDUI `settings_ui.json` plus the params registry), so settings added upstream *or* in your fork
appear in the app automatically.

This repository is the **on-device daemon**. It is designed to be added to an openpilot/sunnypilot fork as
a git submodule at `sunnyconf/`. The Android client lives in
[**Falseclock/sunnyconf-app**](https://github.com/Falseclock/sunnyconf-app) — grab the APK from its
Releases page.

```
your-openpilot-fork/
└── sunnyconf/          ← this repo, as a submodule
    ├── daemon/         stdlib-only Python service: HTTP :8765 + mDNS (_sunnyconf._tcp)
    ├── install.py      wires the daemon into the surrounding checkout (5 small edits)
    ├── scripts/        dev helpers (deploy, smoke test, discovery)
    └── tools/          head-unit helpers (screen recording, fake ignition)
```

The daemon is **stdlib-only** — nothing to `pip install`, which is what makes it deployable on AGNOS
(the comma device image has no writable Python environment).

## Why you need your own fork

The openpilot updater keeps the device an exact mirror of **the remote branch it was installed from**: on
every update it does `reset --hard` + `git clean -xdff` (deleting untracked files) and then
`git submodule update --init --recursive`. Two consequences:

- anything you only copy onto the device is deleted on the next update — the integration has to be
  **committed to the branch your device installs from**;
- once it *is* committed there, updates take care of themselves: the submodule is fetched and initialized
  automatically, on every device that installs your branch.

**This is also true if you drive on the official sunnypilot builds today**: you cannot add sunnyconf to a
repo you don't control, so you need your own fork of sunnypilot with your driving branch + sunnyconf on
top of it. Keeping that fork current is a two-command routine — see
[Keeping up with upstream](#keeping-up-with-upstream-sunnypilot).

## Install

On your PC, in a clone of **your** fork, on the branch your device installs from:

```sh
git submodule add https://github.com/Falseclock/sunnyconf.git sunnyconf
python3 sunnyconf/install.py     # idempotent; prints what it changed
git add -A
git commit -m "add sunnyconf"
git push
```

If `git submodule add` refuses with *"A git directory for 'sunnyconf' is found locally"* — leftovers of an
earlier add/remove cycle — clean them out and retry:

```sh
git submodule deinit -f sunnyconf 2>/dev/null; git rm -rf --cached sunnyconf 2>/dev/null
rm -rf sunnyconf .git/modules/sunnyconf
git config --remove-section submodule.sunnyconf 2>/dev/null
git submodule add https://github.com/Falseclock/sunnyconf.git sunnyconf
```

Then install your branch on the device as usual (custom software URL, or an existing install just
updates itself). After the reboot the daemon is running: it announces itself over mDNS and serves
HTTP on port 8765.

Finally: set a pairing code on the device (Settings → Device → **Sunnyconf Pairing Code**), install the
[app](https://github.com/Falseclock/sunnyconf-app/releases) on your phone (or head unit), and pair.

### What install.py edits

Six files, nothing else — re-running it is always safe. The last column is what happens when an upstream
refactor moves a patch's anchor and it can NOT be applied:

| file | why | if it can't apply |
|---|---|---|
| `system/manager/process_config.py` | registers the managed process (guarded — a missing submodule is skipped, never crash-loops manager) | **install fails** — nothing works without it |
| `common/params_keys.h` | declares `SunnyconfPairingCode` (`PERSISTENT \| DONT_LOG`) | **install fails** — nothing works without it |
| `system/updated/updated.py` | `fetch.recurseSubmodules=false` — a submodule first appearing in fetched commits must not fail the updater's fetch | warns; only future *new* submodules are affected (a fresh install already knows this one) |
| `pyproject.toml` | adds `sunnyconf` to pytest testpaths | warns; dev machines only |
| `selfdrive/ui/sunnypilot/layouts/settings/device.py` | pairing-code button, comma 3/3x UI | warns; set the code over SSH instead (below) |
| `selfdrive/ui/mici/layouts/settings/device.py` | pairing-code button, comma 4 UI | warns; set the code over SSH instead (below) |

If a *best-effort* edit reports `ANCHOR NOT FOUND` (an upstream UI refactor moved things around), the
daemon still works; set the pairing code from a device shell instead:

```python
python3 -c "from openpilot.common.params import Params; Params().put('SunnyconfPairingCode', 'your-code')"
```

## Adding sunnyconf to a device that is already installed

The updater on a device still running pre-sunnyconf code fetches with git's default on-demand submodule
recursion, which fails the whole fetch the moment a commit introduces the new submodule
(`Could not access submodule 'sunnyconf'` → "failed to update"). One-time fix over SSH, then update as usual:

```sh
ssh comma 'git -C /data/openpilot config fetch.recurseSubmodules false'
```

(`install.py` writes the same setting into `system/updated/updated.py`, so once the sunnyconf commit is
installed no device needs this again. A fresh install from your fork's URL never hits it.)

## Keeping up with upstream sunnypilot

Your fork does not update itself — pull upstream into your branch when you want a new release:

```sh
git remote add upstream https://github.com/sunnypilot/sunnypilot.git   # once
git fetch upstream
git merge upstream/master        # or whichever upstream branch/tag you follow
python3 sunnyconf/install.py     # re-applies anything the merge lost; no-op otherwise
git push
```

If the merge conflicts inside one of the five integrated files, take the upstream side and re-run
`install.py` — it re-inserts the integration into the fresh upstream text.

## Updating sunnyconf itself

```sh
git submodule update --remote sunnyconf
python3 sunnyconf/install.py     # in case the new version needs a new integration point
git commit -am "bump sunnyconf"
git push
```

Devices pick the new daemon up with their next normal update.

## Uninstall

The full teardown — the last `rm` matters: `git rm` leaves the submodule's git directory under
`.git/modules/`, and a later re-install trips over it:

```sh
git submodule deinit -f sunnyconf
git rm -f sunnyconf
rm -rf .git/modules/sunnyconf
git checkout -- system/manager/process_config.py common/params_keys.h system/updated/updated.py pyproject.toml selfdrive/ui
git commit -m "remove sunnyconf" && git push
```

## Troubleshooting & collecting logs

Quick health check from anything on the same WiFi (no auth needed):
`http://<device-ip>:8765/health` → `{"ok": true}`.

Everything else over SSH on the device:

```sh
pgrep -af sunnyconf.daemon.main            # is the daemon process alive?
curl -s http://127.0.0.1:8765/health       # does it answer locally?

# daemon log lines (the daemon logs through openpilot's cloudlog into swaglog)
grep -a sunnyconf /data/log/swaglog.* | tail -50        # zcat for the rotated .gz ones

# a crashed managed process leaves a python traceback here — grab the newest file
ls -t /data/community/crashes/ | head -3
tail -40 "/data/community/crashes/$(ls -t /data/community/crashes/ | head -1)"

# device didn't pick up your fork update?
cat /data/params/d/LastUpdateException; echo
cat /data/params/d/UpdaterState; echo

# app can't discover the device? check the mDNS advert is actually out
avahi-browse -rpt _sunnyconf._tcp
```

When opening an issue, include: what you tapped and what happened, `GET /status` output (any browser:
`http://<device-ip>:8765/status` won't work without a token — copy it from the app's device page or use
the swaglog lines instead), the `grep -a sunnyconf` tail, and the newest crash file if there is one.
Client-side (Android) log collection is described in the
[app README](https://github.com/Falseclock/sunnyconf-app#logs).

## Versioning (maintainers)

Fork users never bump anything — the version travels inside the submodule. When changing the daemon:

- **`DAEMON_VERSION`** (`daemon/__init__.py`) — semver: bump the MINOR on any endpoint/contract addition
  or change, the PATCH on fixes (so a bug report's "daemon 1.1.x" identifies the build). It is served in
  `/status`; the app compares it against the oldest daemon its features work with and shows an
  "update daemon on the device" hint when yours is older.
- **`SCHEMA_VERSION`** — bump only if the *shape* of the settings schema contract changes.
- In the app, raise `MIN_DAEMON_VERSION` (`StatusPopover.java`) in the same release that starts relying
  on the new daemon capability.

## Development

- `daemon/tests/` — run with `pytest sunnyconf` from the openpilot root.
- `scripts/` — deploy to a device over SSH, smoke-test the endpoints, mDNS discovery from a PC.
- The HTTP contract the app renders from is documented in the source (`daemon/server.py`,
  `daemon/schema_gen.py`).

## License

MIT — see [LICENSE](LICENSE). sunnypilot and openpilot are their respective projects; this daemon only
reads their param/SDUI declarations at runtime.
