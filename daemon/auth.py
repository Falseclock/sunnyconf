"""
sunnyconf.auth — PIN pairing + per-client bearer tokens.

The AUTH PLACEHOLDER is now real: every endpoint except /health and /pair requires a bearer token.

Design (why it works "once" and survives reboots/updates):
  * The comma keeps a list of authorized clients in  /data/sunnyconf/clients.json .  /data is user data:
    it is NOT touched by the on-device git updater and it survives reboots, so a paired client stays
    paired forever — the pairing code is needed exactly once, at first pairing.
  * Pairing: the user sets a secret pairing code ON THE DEVICE (Settings → Device → SunnyconfPairingCode),
    then types it into the app; POST /pair proves the user chose that code with physical access to the car.
    On success the comma records the client and returns a long-lived token.
  * Each client gets its OWN token (keyed by a client_id the app generates once). That means we can later
    revoke a single device (drop its row) without re-pairing the others. All requests carry
    `Authorization: Bearer <token>`.

The pairing code is the `SunnyconfPairingCode` param — set only on the device screen (guarded from remote
writes via BLOCKED_PARAMS/SECRET_PARAMS in server.py). If it is unset, pairing is DISABLED. Brute force is
bounded by a per-IP rate limiter (a short code + open /pair would otherwise be guessable). pin_ok() takes the
expected code as an argument so this module keeps no openpilot imports and stays unit-testable.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

_CLIENTS = "clients.json"
_lock = threading.Lock()

# /pair brute-force guard (per client IP): after _MAX_FAILS wrong codes, lock out for _LOCKOUT seconds.
_MAX_FAILS = 5
_LOCKOUT = 60.0
_fail_state: dict = {}  # ip -> {"fails": int, "until": float}


def state_dir() -> Path:
  """Persistent, writable per-device state directory. /data survives reboots and the git updater."""
  for cand in (os.environ.get("SUNNYCONF_STATE_DIR"), "/data/sunnyconf"):
    if not cand:
      continue
    try:
      p = Path(cand)
      p.mkdir(parents=True, exist_ok=True)
      if os.access(p, os.W_OK):
        return p
    except OSError:
      pass
  p = Path.home() / ".comma" / "sunnyconf"
  p.mkdir(parents=True, exist_ok=True)
  return p


def code_set(expected_code) -> bool:
  """True if the user has configured a pairing code on the device (else pairing is disabled)."""
  return bool(expected_code) and len(str(expected_code)) > 0


def pin_ok(pin, expected_code) -> bool:
  """Constant-time compare of the submitted code against the device's SunnyconfPairingCode."""
  if not pin or not expected_code:
    return False
  return hmac.compare_digest(str(pin), str(expected_code))


# -- /pair brute-force rate limiter (per IP) ------------------------------------------------------
def rate_limited(ip) -> int:
  """Seconds the caller must wait before another /pair attempt, or 0 if allowed now."""
  now = time.time()
  with _lock:
    s = _fail_state.get(ip)
    if s and s["until"] > now:
      return int(s["until"] - now) + 1
  return 0


def record_fail(ip) -> None:
  now = time.time()
  with _lock:
    s = _fail_state.get(ip, {"fails": 0, "until": 0.0})
    s["fails"] = s.get("fails", 0) + 1
    if s["fails"] >= _MAX_FAILS:
      s["until"] = now + _LOCKOUT
      s["fails"] = 0
    _fail_state[ip] = s


def clear_fails(ip) -> None:
  with _lock:
    _fail_state.pop(ip, None)


# -- client store (thread-safe; the HTTP server is threaded) --------------------------------------
def _clients_path() -> Path:
  return state_dir() / _CLIENTS


def _load() -> list:
  try:
    data = json.loads(_clients_path().read_text())
    return data if isinstance(data, list) else []
  except (OSError, ValueError):
    return []


def _save(clients: list) -> None:
  path = _clients_path()
  tmp = path.with_suffix(".tmp")
  tmp.write_text(json.dumps(clients, indent=2))
  os.replace(tmp, path)
  try:
    os.chmod(path, 0o600)
  except OSError:
    pass


def _bearer(auth_header: str | None) -> str | None:
  if not auth_header:
    return None
  parts = auth_header.split()
  if len(parts) != 2 or parts[0].lower() != "bearer":
    return None
  return parts[1]


def token_ok(auth_header: str | None) -> bool:
  """True if the request carries a bearer token belonging to a paired client."""
  tok = _bearer(auth_header)
  if not tok:
    return False
  with _lock:
    for c in _load():
      if hmac.compare_digest(str(c.get("token", "")), tok):
        return True
  return False


def pair(client_id: str | None, label: str | None) -> dict:
  """Register (or refresh) a client and mint its token. Caller must have verified the PIN.

  Keyed by client_id so re-pairing the same device replaces its row instead of piling up. Returns the
  client record (including the token) to hand back to the app."""
  cid = (client_id or secrets.token_hex(8)).strip()[:64]
  lbl = (label or "device").strip()[:64]
  token = secrets.token_urlsafe(24)
  record = {"client_id": cid, "label": lbl, "token": token, "paired_at": int(time.time())}
  with _lock:
    clients = [c for c in _load() if c.get("client_id") != cid]
    clients.append(record)
    _save(clients)
  return record


def list_clients() -> list:
  """Public view of paired clients (never leak the token)."""
  with _lock:
    return [{"client_id": c.get("client_id"), "label": c.get("label"),
             "paired_at": c.get("paired_at")} for c in _load()]


def revoke(client_id: str) -> bool:
  """Drop a single client so it must pair again. Returns True if something was removed."""
  with _lock:
    clients = _load()
    kept = [c for c in clients if c.get("client_id") != client_id]
    if len(kept) == len(clients):
      return False
    _save(kept)
    return True
