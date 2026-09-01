#!/usr/bin/env python3
"""sunnyconf integration installer.

Wires the sunnyconf daemon (this submodule) into the openpilot/sunnypilot checkout that contains it.
Run from anywhere AFTER adding the submodule:

    git submodule add https://github.com/Falseclock/sunnyconf.git sunnyconf
    python3 sunnyconf/install.py
    git add -A && git commit -m "add sunnyconf"

Idempotent: every edit is checked for before it is applied, so re-running (e.g. after merging a new
upstream sunnypilot release) only re-applies what a merge lost and leaves the rest untouched.

What it edits (and nothing else):
  REQUIRED — the daemon does not run without these:
    system/manager/process_config.py   register the managed process (guarded: a missing/uninitialized
                                       submodule is skipped, it never crash-loops the manager)
    common/params_keys.h               declare the SunnyconfPairingCode param
  BEST-EFFORT — nice to have; a UI refactor upstream may move the anchors, the README documents
  the manual equivalent:
    system/updated/updated.py                             fetch must not recurse into submodules
    pyproject.toml                                        add daemon tests to pytest testpaths
    selfdrive/ui/sunnypilot/layouts/settings/device.py    "Sunnyconf Pairing Code" button (comma 3/3x UI)
    selfdrive/ui/mici/layouts/settings/device.py          "pairing code" button (comma 4 UI)

Exit code 0 = all required edits in place (already or newly). 1 = a required anchor was not found.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # this file lives in <openpilot>/sunnyconf/

OK, SKIP, FAIL = "applied", "already in place", "ANCHOR NOT FOUND"


def patch(rel_path, edits, required):
  """edits: list of (anchor, insert, where[, probe]) — where is 'before' or 'after' the first anchor
  occurrence. Each edit is independently idempotent: skipped when its probe (default: the insert text
  itself) is already in the file. Give an explicit probe when only part of the insert is load-bearing
  (e.g. the code line, not the comment above it), so a hand-made or older-format integration is
  recognized instead of duplicated."""
  path = os.path.join(ROOT, rel_path)
  if not os.path.exists(path):
    print(f"[{'FAIL' if required else 'warn'}] {rel_path}: file not found")
    return not required
  with open(path, encoding="utf-8") as f:
    text = f.read()
  results, ok = [], True
  for anchor, insert, where, *rest in edits:
    probe = rest[0] if rest else insert.strip()
    if probe in text:
      results.append(SKIP)
      continue
    i = text.find(anchor)
    if i < 0:
      results.append(FAIL)
      ok = False
      continue
    pos = i if where == "before" else i + len(anchor)
    text = text[:pos] + insert + text[pos:]
    results.append(OK)
  if any(r == OK for r in results):
    with open(path, "w", encoding="utf-8") as f:
      f.write(text)
  status = "FAIL" if not ok and required else ("warn" if not ok else " ok ")
  print(f"[{status}] {rel_path}: " + ", ".join(results))
  return ok or not required


def main():
  if not os.path.exists(os.path.join(ROOT, "system", "manager", "process_config.py")):
    sys.exit(f"error: {ROOT} does not look like an openpilot checkout — is the submodule at <openpilot>/sunnyconf/?")

  ok = True

  # ── REQUIRED ────────────────────────────────────────────────────────────────────────────────────────

  # 1. managed process. Appended right before the procs dict is built, guarded by the submodule actually
  #    being checked out — `submodule update --init` not having run yet must not crash-loop manager.
  ok &= patch("system/manager/process_config.py", [(
    "managed_processes = {p.name: p for p in procs}",
    '# sunnyconf — local schema-driven config daemon (HTTP + mDNS over WiFi). Runs onroad AND offroad so\n'
    '# settings stay reachable on a parked car. Registered only when the submodule is checked out, so a\n'
    '# missing/uninitialized sunnyconf/ never crash-loops the manager.\n'
    'if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sunnyconf", "daemon", "main.py")):\n'
    '  procs.append(PythonProcess("sunnyconf", "sunnyconf.daemon.main", always_run, restart_if_crash=True))\n\n',
    "before",
    'PythonProcess("sunnyconf"',  # an older/hand-made registration counts as integrated
  )], required=True)

  # 2. pairing-code param. DONT_LOG: it is a secret; PERSISTENT: survives reboots.
  ok &= patch("common/params_keys.h", [(
    "\n};",
    '\n\n    // sunnyconf (WiFi config daemon — https://github.com/Falseclock/sunnyconf)\n'
    '    {"SunnyconfPairingCode", {PERSISTENT | DONT_LOG, STRING}},',
    "before",
    '"SunnyconfPairingCode"',  # the key itself is what matters, not the comment above it
  )], required=True)

  # ── BEST-EFFORT ─────────────────────────────────────────────────────────────────────────────────────

  # 2b. updater: git's default on-demand submodule recursion fails the whole fetch when a submodule first
  #     APPEARS in the fetched commits ("Could not access submodule 'sunnyconf'"). updated.py handles
  #     submodules explicitly after checkout, so fetch must not recurse. NOTE: this protects updates AFTER
  #     this code is installed — a device still running pre-sunnyconf code needs the one-time command from
  #     README.md § "Adding sunnyconf to a device that is already installed".
  patch("system/updated/updated.py", [(
    '    ("gc.autoDetach", "false"),\n',
    '    # a submodule that first appears in the fetched commits (e.g. sunnyconf) is unknown to the current\n'
    '    # checkout, and the default on-demand recursion then fails the whole fetch. Submodules are handled\n'
    '    # explicitly after checkout (submodule sync + update --init --recursive); fetch must not recurse.\n'
    '    ("fetch.recurseSubmodules", "false"),\n',
    "after",
    "fetch.recurseSubmodules",
  )], required=False)

  # 3. pytest testpaths (dev machines only; harmless if it fails)
  patch("pyproject.toml", [(
    '  "sunnypilot",\n',
    '  "sunnyconf",\n',
    "after",
  )], required=False)

  # 4. comma 3/3x settings UI: Device page button that sets the pairing code on-screen.
  #    Manual fallback if an upstream refactor moves these anchors: README.md § "Setting the pairing code".
  patch("selfdrive/ui/sunnypilot/layouts/settings/device.py", [
    (
      "from openpilot.system.ui.lib.multilang import tr",
      "from openpilot.system.ui.sunnypilot.widgets.input_dialog import InputDialogSP\n",
      "before",
    ),
    (
      "    items = [",
      '    # sunnyconf pairing code — the secret you type into the sunnyconf app to pair a phone/head unit over Wi-Fi\n'
      '    self._pairing_code_btn = button_item_sp(\n'
      '      lambda: tr("Sunnyconf Pairing Code"),\n'
      '      lambda: tr("CHANGE") if (self._params.get("SunnyconfPairingCode") or "") else tr("SET"),\n'
      '      description=lambda: tr("Code you type into the sunnyconf app to pair a device over Wi-Fi. "\n'
      '                             "Leave empty to disable pairing."),\n'
      '      callback=self._set_pairing_code,\n'
      '    )\n\n',
      "before",
      '_pairing_code_btn = button_item_sp(',
    ),
    (
      'button_item_sp(lambda: tr("Change Language"), lambda: tr("CHANGE"), callback=self._show_language_dialog),\n'
      '      LineSeparator(),\n',
      '      self._pairing_code_btn,\n'
      '      LineSeparator(),\n',
      "after",
    ),
    (
      "  def _update_state(self):",
      '  def _set_pairing_code(self):\n'
      '    # Keyboard writes SunnyconfPairingCode on CONFIRM (InputDialogSP handles the put via param=). The daemon\n'
      '    # reads it as the pairing secret; password_mode hides it as you type since it grants config access.\n'
      '    InputDialogSP(\n'
      '      title=tr("Sunnyconf Pairing Code"),\n'
      '      sub_title=tr("Enter this code in the sunnyconf app to pair a device"),\n'
      '      current_text=self._params.get("SunnyconfPairingCode") or "",\n'
      '      param="SunnyconfPairingCode",\n'
      '      password_mode=True,\n'
      '    ).show()\n\n',
      "before",
      'def _set_pairing_code(',
    ),
  ], required=False)

  # 5. comma 4 (mici) settings UI: same button, BigButton style.
  patch("selfdrive/ui/mici/layouts/settings/device.py", [
    (
      # BigInputDialog exists in the tree but stock device.py doesn't import it — without this line the
      # button below dies with NameError on tap (caught in the field 2026-09-01). A redundant standalone
      # import on trees that already have it combined into another import line is harmless.
      "from openpilot.selfdrive.ui.mici.widgets.pairing_dialog import PairingDialog",
      "from openpilot.selfdrive.ui.mici.widgets.dialog import BigInputDialog\n",
      "before",
    ),
    (
      "    self._scroller.add_widgets([",
      '    # sunnyconf pairing code — secret typed into the sunnyconf app to pair a device over Wi-Fi\n'
      '    pairing_code_btn = BigButton("pairing\\ncode",\n'
      '                                 "Set" if (ui_state.params.get("SunnyconfPairingCode") or "") else "Not set",\n'
      '                                 gui_app.texture("icons_mici/settings/device/info.png", 64, 64))\n\n'
      '    def _open_pairing_code():\n'
      '      def _save(code):\n'
      '        ui_state.params.put("SunnyconfPairingCode", code or "")\n'
      '        pairing_code_btn.set_value("Set" if code else "Not set")\n'
      '      cur = ui_state.params.get("SunnyconfPairingCode") or ""\n'
      '      gui_app.push_widget(BigInputDialog("enter pairing code...", cur, minimum_length=0, confirm_callback=_save))\n'
      '    pairing_code_btn.set_click_callback(_open_pairing_code)\n\n',
      "before",
      'pairing_code_btn.set_click_callback(',
    ),
    (
      "      PairBigButton(),\n",
      "      pairing_code_btn,\n",
      "after",
    ),
  ], required=False)

  if not ok:
    print("\nA REQUIRED edit failed — the daemon will not run. Apply it by hand (see README.md) or open an issue.")
    sys.exit(1)
  print("\nDone. Review with `git diff`, then commit and push to the branch your device installs from:")
  print('  git add -A && git commit -m "add sunnyconf" && git push')


if __name__ == "__main__":
  main()
