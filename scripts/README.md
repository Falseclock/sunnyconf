# sunnyconf test scripts

Reusable toolkit for testing the daemon on a comma device over SSH from the dev box (WSL). Every script
sources [`lib.sh`](lib.sh); point it at a device with the `comma` ssh alias (in `~/.ssh/config`) or an env
override — e.g.:

```bash
SUNNYCONF_HOST=comma4 bash sunnyconf/scripts/smoke.sh    # any alias or user@ip
```

Defaults: host = ssh alias **`comma`** (you define it), key from your ssh_config/agent, tree `/data/openpilot`,
python `/usr/local/venv/bin/python3` (AGNOS venv — deps aren't in bare `/usr/bin/python3`), port `8765`.

## Conventions — keep it universal (rules)

Nothing in sunnyconf is bound to a specific device, IP, key, hostname, or model. When adding code or scripts,
**parameterize — never embed a device IP / hostname / dongle-serial / model literal.**
- **Device model** is auto-detected from hardware (`HARDWARE.get_device_type()` → comma3 / comma3x / comma4);
  the mDNS instance name + `/status` + TXT reflect the real device. Only `daemon/device.py` maps type→name.
- **SSH target** comes from `$SUNNYCONF_HOST` (default alias `comma`) and `$SUNNYCONF_KEY` (default: ssh_config).
  No IPs or key paths in the scripts.
- **Discovery** (`discover.py`) browses the service type and finds whatever comma is on the LAN — no fixed IP.
- **Paths / python** use `$OP_DIR` and `$REMOTE_PY` (standard AGNOS locations), overridable via env.

## Daily flow

```bash
bash sunnyconf/scripts/deploy.sh          # rsync sunnyconf/ onto the device (no push, no branch switch)
bash sunnyconf/scripts/daemon.sh start    # run the daemon standalone (alongside a running openpilot)
bash sunnyconf/scripts/smoke.sh           # curl /health /status /schema (read-only)
bash sunnyconf/scripts/smoke.sh write     # + one offroad-safe PUT round-trip
bash sunnyconf/scripts/daemon.sh log      # follow the daemon log
bash sunnyconf/scripts/daemon.sh stop     # stop it
```

## Discovery (mDNS "do we see the comma?")

```powershell
# from Windows (native multicast — verified working):
python3 sunnyconf/scripts/discover.py 8
```
```bash
bash sunnyconf/scripts/discover.sh device   # cross-check via the comma's own avahi
```
**Do not** run `discover.py` under WSL — WSL2 doesn't receive LAN multicast. Use Windows/macOS/Linux native,
the device-avahi cross-check, or the phone (`NsdManager`). `discover.py` binds :5353 and joins the group on
every local interface (needed on multi-homed Windows).

## Device management

```bash
bash sunnyconf/scripts/device.sh status         # branch / service / tmux / offroad / DisableUpdates / prebuilt
bash sunnyconf/scripts/device.sh updates-off    # DisableUpdates=1 (edits survive reboot) + stop updater
bash sunnyconf/scripts/device.sh updates-on     # re-enable OTA
bash sunnyconf/scripts/device.sh quickboot-on   # touch prebuilt (skip build.py on boot)
bash sunnyconf/scripts/device.sh op-stop|op-start|op-restart   # openpilot (comma.service / tmux 'comma')
bash sunnyconf/scripts/device.sh op-attach      # attach openpilot tmux (interactive terminal only)
bash sunnyconf/scripts/device.sh shell          # shell on the device in /data/openpilot
```

Notes:
- The daemon binds `:8765`, which openpilot doesn't use — so **no need to stop openpilot** for API/curl/mDNS
  tests. Stop it only for the *managed-process* path.
- `op-attach` / `shell` need a real interactive terminal (not the automated runner).
- To test the managed-process path (the manager launches `sunnyconf.daemon.main` itself), switch the device
  to the committed `sunnyconf` branch (it carries the `process_config.py` registration) and `op-restart`.

See [`../README.md`](../README.md) → "Running & testing on a comma device" for the full explanation
(the `DisableUpdates` / `prebuilt` flags, start/stop internals, cleanup).
