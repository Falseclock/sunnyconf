"""
sunnyconf.server — stdlib HTTP API for the config daemon.

Chosen stack: http.server.ThreadingHTTPServer (Python standard library). Rationale (see
INVESTIGATION.md): the on-device updater never runs pip/uv, so a new third-party dep would not reach
the car; the stdlib is guaranteed present. The surface is tiny (a handful of endpoints) and the
synchronous handler pairs naturally with the synchronous Params() API.

Auth: every endpoint except /health and /pair requires `Authorization: Bearer <token>` (see auth.py).
Pair once with the PIN shown on the comma screen; the client then holds a long-lived token.

Endpoints:
  GET  /health                 -> {"ok": true}                       (open)
  POST /pair  {pin,client_id,label} -> {token, ...} | 401 bad_pin    (open; proves physical access)
  GET  /status                 -> {onroad, offroad, model, dongle_id, schema_version, ...}
  GET  /schema[?refresh=1][&all=1]  -> the settings schema (the client renders the menu from this)
  GET  /values[?all=1]         -> {key: "<current value>"} for every key in the schema
  GET  /params/<key>           -> {key, value, meta}
  PUT  /params/<key>  {"value"}-> validate + write; 409 if offroad_only and onroad; 403 if blocked
  DELETE /params/<key>         -> remove an allowlisted EXTRA_KEYS param (the OSM flows need remove semantics)
  GET  /maps                   -> Maps/OSM page state: mapd version, size, download progress, selection
  GET  /maps/check             -> is the map-data server newer than our download? {update_available, ...}
  GET  /drives                 -> recorded routes (watcher-indexer runs continuously) + indexing progress
  GET  /drives/live            -> is a drive recording right now + started/elapsed/distance so far
  GET  /drives/<route>/track   -> merged GPS points + engagement bar + event markers (app-ready)
  GET  /drives/<route>/thumb/<seg>     -> per-minute road JPEG (filmstrip)
  GET  /drives/<route>/playlist.m3u8   -> HLS playlist of the route's qcamera segments (+ /qcam/<seg>.ts)
  DELETE /drives/<route>       -> delete a drive's recordings + cache (409 while it's being recorded)
  PUT  /maps  {"countries"}    -> set the (multi-)country map selection [{"ref","title"},...]
  DELETE /maps/<ref>           -> remove ONE country from the selection + delete its tiles (geometry-aware)
  POST /actions/<name>         -> device actions (reboot, ..., delete_maps, osm_update)
  GET  /clients                -> [{client_id, label, paired_at}]    (manage paired devices)
  DELETE /clients/<client_id>  -> {ok, removed} (un-pair a device)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.system.hardware.hw import Paths
from . import BLOCKED_PARAMS, DAEMON_VERSION, DEFAULT_PORT, SCHEMA_VERSION
from . import auth
from . import device
from . import drives
from . import schema_gen
from . import values

_WRITABLE_TYPES = {"BOOL", "INT", "FLOAT", "STRING"}

# Params whose VALUE must never leave the device (not readable via /values or /params/<key>).
SECRET_PARAMS = frozenset({"SunnyconfPairingCode"})

# Params the native app's custom pages need (Vehicle selector, Models selector) that are NOT part of the
# SDUI settings schema, so they aren't in schema_gen's index. Served through /params/<key> via this
# allowlist. `w` = writable (car/model selection), else read-only; `offroad` = reject writes while onroad.
# type is the registry type_token used by values.current_transport / Params.put. Mirrors how the web app
# reads/writes these same params through the generic settings API.
EXTRA_KEYS = {
  "CarList":               {"type": "STRING", "w": False},                    # supported platforms (device-built)
  "CarPlatformBundle":     {"type": "STRING", "w": True, "offroad": True},     # manual car selection (JSON)
  "CarFingerprint":        {"type": "STRING", "w": False},                     # auto-detected platform
  "ModelManager_ModelsCache":   {"type": "STRING", "w": False},               # available model bundles (manifest, snake_case)
  "ModelManager_ActiveBundle":  {"type": "STRING", "w": True, "offroad": True},# active model bundle (JSON, camelCase); write "{}" to reset to Default
  "ModelManager_DownloadIndex": {"type": "INT",    "w": True, "offroad": True},# write a bundle index -> manager downloads + activates it; read = which index is downloading
  "ModelManager_ClearCache":    {"type": "BOOL",   "w": True, "offroad": True},# write "1" -> manager deletes cached model files (keeps the active one)
  "ModelManager_LastSyncTime":  {"type": "INT",    "w": True},                # write "0" -> manager refetches the model manifest
  "ModelManager_Favs":          {"type": "STRING", "w": True, "offroad": True},# favourited bundles (";"-joined refs)
  # Maps (OSM) page — mirrors the params the stock OSM panel reads/writes
  # (selfdrive/ui/sunnypilot/layouts/settings/osm.py). No onroad gate: the on-device panel has none either.
  "MapdVersion":       {"type": "STRING", "w": False},   # written by mapd_installer
  "OsmLocationName":   {"type": "STRING", "w": True},    # country ref, e.g. "US"
  "OsmLocationTitle":  {"type": "STRING", "w": True},    # country display name
  "OsmStateName":      {"type": "STRING", "w": True},    # US state ref (or "All")
  "OsmStateTitle":     {"type": "STRING", "w": True},    # US state display name
  "OsmLocal":          {"type": "BOOL",   "w": True},    # "maps selected" flag (drives the update-required alert)
  "OsmDbUpdatesCheck": {"type": "BOOL",   "w": True},    # write true -> mapd_manager starts the download cycle
  "OsmDownloadedDate": {"type": "STRING", "w": False},   # unix ts written by mapd_manager ("Last checked …")
}

def _extra_coerce(et: str, val):
  """Coerce a wire value to the Python type Params.put expects for an EXTRA_KEYS param: JSON-typed params
  (CarPlatformBundle / ModelManager_ActiveBundle) want a dict/list, INT params want an int (ValueError
  propagates to the caller), BOOL params want a bool. Plain STRING params pass through unchanged."""
  if isinstance(val, str):
    s = val.strip()
    if s[:1] in ("{", "["):
      try:
        return json.loads(s)
      except Exception:
        return val
    if et == "INT":
      return int(s)
    if et == "BOOL":
      return s in ("1", "true", "True", "yes", "on")
  return val


# Offline OSM maps live here (same constant as the stock OSM panel); size + delete work on this tree.
MAP_PATH = Path(Paths.mapd_root()) / "offline"

# mapd publishes its live download state to the mem-params root (see sunnypilot/mapd/mapd_manager.py):
# OSMDownloadLocations non-empty == a download is in flight. Lazy so a dev machine without /dev/shm still imports.
_mem_params_obj: Params | None = None


def _mem_params() -> Params:
  global _mem_params_obj
  if _mem_params_obj is None:
    _mem_params_obj = Params("/dev/shm/params")
  return _mem_params_obj


# sunnyconf extension: the FULL multi-country selection, [{"ref","title"},...]. mapd natively takes a nations
# LIST; only the stock UI is single-country. Kept in the daemon's own state dir (like clients.json) — NOT a
# param — so no registry/recompile is needed. First entry mirrors OsmLocationName/Title so the on-device
# single-country OSM panel stays coherent. Written via PUT /maps; downloads go through POST /actions/osm_update.
def _osm_countries_path():
  return auth.state_dir() / "osm_countries.json"


def _load_osm_countries() -> list[dict]:
  try:
    data = json.loads(_osm_countries_path().read_text())
    out = []
    for c in data if isinstance(data, list) else []:
      if isinstance(c, dict) and (c.get("ref") or "").strip():
        out.append({"ref": str(c["ref"]).strip(), "title": str(c.get("title") or c["ref"])})
    return out
  except (OSError, ValueError):
    return []


def _save_osm_countries(countries: list[dict]) -> None:
  path = _osm_countries_path()
  if not countries:
    try:
      path.unlink()
    except OSError:
      pass
    return
  tmp = path.with_suffix(".tmp")
  tmp.write_text(json.dumps(countries, indent=2))
  tmp.replace(path)


def _mirror_selection(p: Params, countries: list[dict]) -> None:
  """Keep the stock single-country params coherent with the multi-selection: FIRST country mirrors into
  OsmLocationName/Title + OsmLocal (what the on-device panel and mapd_manager read); clear the state params
  when US isn't selected; clear everything when the selection is empty (the stock US-cancel path)."""
  if countries:
    p.put_bool("OsmLocal", True)
    p.put("OsmLocationName", countries[0]["ref"])
    p.put("OsmLocationTitle", countries[0]["title"])
    if not any(c["ref"] == "US" for c in countries):
      p.remove("OsmStateName")
      p.remove("OsmStateTitle")
  else:
    for k in ("OsmLocationName", "OsmLocationTitle", "OsmStateName", "OsmStateTitle", "OsmLocal"):
      p.remove(k)


def _osm_selection(p: Params) -> tuple[list[str], list[str]]:
  """The nations/states list to hand mapd: the saved multi-country selection with OsmLocationName as the
  single-country fallback (device-panel selection), filtered with the same US/All rules as
  mapd_manager.filter_nations_and_states (inline so the daemon doesn't import the manager's live-map deps)."""
  nations: list[str] = []
  for c in _load_osm_countries():
    if c["ref"] not in nations:
      nations.append(c["ref"])
  single = (p.get("OsmLocationName") or "").strip()
  if not nations and single:
    nations = [single]
  states: list[str] = []
  if "US" in nations:
    state = (p.get("OsmStateName") or "").strip() or "All"
    if state.lower() == "all":
      states = []                # whole US: keep "US" in nations, no explicit state
    else:
      nations.remove("US")       # specific state: the state download replaces the whole-US one
      states = [state]
  return nations, states


# mapd serves the offline tiles from this host (the URL baked into the mapd binary); each tile answers a HEAD
# with Last-Modified = when pfeiferj last regenerated the dataset. Sampling a few of OUR tiles and comparing
# the newest remote Last-Modified against OsmDownloadedDate tells whether an update would actually fetch
# anything newer — that's what /maps/check does (the stock CHECK just re-downloads unconditionally).
_MAP_DATA_BASE = "https://map-data.pfeifer.dev/"


def _osm_sample_tiles(limit: int = 3) -> list[str]:
  """A few downloaded tile paths relative to the osm root (e.g. 'offline/42/74/42.75..._74.5...'), spread
  across the tree (first/middle/last of the sorted list) so one stale mirror region can't fool the check."""
  tiles: list[str] = []
  stack = [MAP_PATH] if MAP_PATH.exists() else []
  while stack:
    try:
      for entry in os.scandir(stack.pop()):
        if entry.is_file():
          tiles.append(entry.path)
        elif entry.is_dir():
          stack.append(entry.path)
    except OSError:
      pass
  if not tiles:
    return []
  tiles.sort()
  picks = {0, len(tiles) // 2, len(tiles) - 1}
  root = str(Path(MAP_PATH).parent)
  return [os.path.relpath(tiles[i], root).replace(os.sep, "/") for i in sorted(picks)]


def _osm_remote_date() -> tuple[float | None, int]:
  """Newest Last-Modified among sampled tiles on the map-data server (unix ts), and how many were sampled.
  None if nothing is downloaded yet or the server didn't answer."""
  import urllib.request
  from email.utils import parsedate_to_datetime
  newest: float | None = None
  sampled = 0
  for rel in _osm_sample_tiles():
    try:
      req = urllib.request.Request(_MAP_DATA_BASE + rel, method="HEAD")
      with urllib.request.urlopen(req, timeout=6) as resp:
        lm = resp.headers.get("Last-Modified")
      if lm:
        ts = parsedate_to_datetime(lm).timestamp()
        newest = ts if newest is None else max(newest, ts)
        sampled += 1
    except Exception:
      continue
  return newest, sampled


# Per-country delete needs geometry: the offline tree is a 0.25° tile grid (filename = the tile's own
# min_lat_min_lon_max_lat_max_lon), NOT per-country folders, and country bounding boxes overlap (Kyrgyzstan's
# box sits inside Kazakhstan's). Removing country X = delete the tiles that intersect X's box and do NOT
# intersect any remaining country's box — so shared tiles survive and nothing needs re-downloading.
_BBOX_URL = "https://raw.githubusercontent.com/pfeiferj/openpilot-mapd/main/nation_bounding_boxes.json"
_bbox_cache: dict[str, tuple[float, float, float, float]] | None = None


def _nation_bboxes() -> dict[str, tuple[float, float, float, float]]:
  """ref -> (min_lat, min_lon, max_lat, max_lon), from the same GitHub JSON the pickers use. Cached for the
  daemon's lifetime; {} (uncached) when the fetch fails so a later retry can succeed."""
  global _bbox_cache
  if _bbox_cache is None:
    import urllib.request
    try:
      with urllib.request.urlopen(_BBOX_URL, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
      boxes = {}
      for ref, v in data.items():
        bb = (v or {}).get("bounding_box") or {}
        boxes[ref] = (float(bb["min_lat"]), float(bb["min_lon"]), float(bb["max_lat"]), float(bb["max_lon"]))
      _bbox_cache = boxes
    except Exception:
      cloudlog.exception("sunnyconf: nation bounding boxes unavailable")
      return {}
  return _bbox_cache


def _tile_bbox(name: str) -> tuple[float, float, float, float] | None:
  """'41.500000_66.250000_41.750000_66.500000' -> (min_lat, min_lon, max_lat, max_lon)."""
  parts = name.split("_")
  if len(parts) != 4:
    return None
  try:
    return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
  except ValueError:
    return None


def _bbox_intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
  return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


# The published bounding boxes slightly undershoot the real tile sets (mapd's per-nation lists are
# polygon-based; border-fringe tiles fall just outside the JSON box). Pad the boxes when matching tiles so
# removals and per-country sizes catch the fringe instead of leaving orphan tiles no row accounts for.
_BBOX_PAD = 0.3


def _padded(bb: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
  return (bb[0] - _BBOX_PAD, bb[1] - _BBOX_PAD, bb[2] + _BBOX_PAD, bb[3] + _BBOX_PAD)


_maps_size_cache: tuple[float, int, dict] = (0.0, 0, {})   # (monotonic ts, total bytes, {ref: exclusive bytes})


def _maps_inventory(countries: list[dict]) -> tuple[int, dict[str, int]]:
  """One scandir walk (cached 5s): total bytes under MAP_PATH plus, per selected country, the EXCLUSIVE bytes
  — tiles inside only that country's bounding box. That's exactly what removing the country would free (boxes
  overlap: Kazakhstan's covers most of Kyrgyzstan, so KG's exclusive share is small), shown in its list row."""
  global _maps_size_cache
  now = time.monotonic()
  if now - _maps_size_cache[0] < 5.0:
    return _maps_size_cache[1], _maps_size_cache[2]
  boxes = _nation_bboxes() if countries else {}
  refs = [(c["ref"], _padded(boxes[c["ref"]])) for c in countries if c["ref"] in boxes]
  total = 0
  excl: dict[str, int] = {ref: 0 for ref, _ in refs}
  stack = [MAP_PATH] if MAP_PATH.exists() else []
  while stack:
    try:
      for entry in os.scandir(stack.pop()):
        if entry.is_dir():
          stack.append(entry.path)
          continue
        if not entry.is_file():
          continue
        size = entry.stat().st_size
        total += size
        if refs:
          tb = _tile_bbox(entry.name)
          if tb is not None:
            owners = [ref for ref, bb in refs if _bbox_intersects(tb, bb)]
            if len(owners) == 1:
              excl[owners[0]] += size
    except OSError:
      pass
  _maps_size_cache = (now, total, excl)
  return total, excl


def _maps_size() -> int:
  return _maps_inventory(_load_osm_countries())[0]

PAIRING_CODE_KEY = "SunnyconfPairingCode"

# Device actions — each mirrors exactly what the matching button on the device's own Device panel does
# (selfdrive/ui/mici/layouts/settings/device.py, selfdrive/ui/sunnypilot/layouts/settings/device.py), incl.
# the same offroad gating the on-device buttons apply (power off is only offered without ignition; driver
# preview / reset are offroad-only). Actions are not settings, so they are deliberately not in /schema.
ACTIONS = {
  # gate mirrors the device's own Device panel (see sunnyconf/docs/PORTING_PLAYBOOK.md §4):
  #   "engaged" -> blocked while openpilot/MADS is engaged ("Disengage to …")
  #   "offroad" -> allowed only offroad
  "reboot":            {"gate": "engaged"},
  "poweroff":          {"gate": "engaged"},
  "reset_calibration": {"gate": "engaged"},
  "reset_settings":    {"gate": "offroad"},
  # Maps page: delete all downloaded OSM maps (stock _do_delete_maps) — refused while a download is running,
  # mirroring the stock panel disabling DELETE during a download.
  "delete_maps":       {"gate": "none"},
  # Maps page: start/refresh the map download for the SELECTED COUNTRIES (SunnyconfOsmCountries, fallback
  # OsmLocationName) — the daemon-side twin of mapd_manager.request_refresh_osm_location_data(), but with the
  # full multi-country list. Refused while a download is already running.
  "osm_update":        {"gate": "none"},
}

# delete_maps removes these after wiping MAP_PATH (the exact list the stock OSM panel removes)
_OSM_RESET_KEYS = ("OsmDownloadedDate", "OsmLocal", "OsmLocationName", "OsmLocationTitle",
                   "OsmStateName", "OsmStateTitle")
# No driver-camera action: the device's button just pushes a dialog on its own screen, and the
# IsDriverViewEnabled param is that dialog's own state (it sets it on show, clears it on hide) — not a
# trigger, so setting it over the network does nothing. See selfdrive/ui/onroad/driver_camera_dialog.py.

# reset calibration wipes these, then asks for an onroad cycle (same list as the device UI)
_CALIBRATION_KEYS = ("CalibrationParams", "LiveTorqueParameters", "LiveParameters", "LiveParametersV2", "LiveDelay")


# Endpoints reachable without a token: liveness (for discovery) and pairing (guarded by the PIN instead).
_OPEN_PATHS = frozenset({"/health", "/pair"})


def authorized(method):
  """Single hook guarding every handler. All paths except _OPEN_PATHS require a paired client's
  bearer token; /pair is instead guarded by the on-screen PIN (see auth.py).

  Media URLs may carry the token as `?t=` instead of the header: Android's MediaPlayer drops the custom
  headers it was given when it re-opens the connection to seek (KitKat head unit — the seek came back
  401 and playback silently rolled back), and the URL is the only thing it keeps. Same token, same
  local-network trust; it just survives the player's own re-request."""
  @wraps(method)
  def wrapper(self, *args, **kwargs):
    parsed = urlparse(self.path)
    path = parsed.path.rstrip("/") or "/"
    if path in _OPEN_PATHS:
      return method(self, *args, **kwargs)
    if auth.token_ok(self.headers.get("Authorization")):
      return method(self, *args, **kwargs)
    qtok = (parse_qs(parsed.query).get("t") or [""])[0]
    if qtok and auth.token_ok("Bearer " + qtok):
      return method(self, *args, **kwargs)
    return self._send(401, {"error": "unauthorized"})
  return wrapper


def is_onroad(params: Params) -> bool:
  """Canonical headless onroad check: the manager maintains IsOnroad/IsOffroad (see INVESTIGATION.md).
  We are launched by the manager, which writes offroad params before we run, so IsOnroad is current."""
  try:
    return bool(params.get_bool("IsOnroad"))
  except Exception:
    return False


_tls = threading.local()   # per-thread SubMaster; zmq sockets are not thread-safe across request threads


def is_engaged(params: Params) -> bool:
  """Headless mirror of ui_state.engaged (ui_state.py):
        started AND (selfdriveState.enabled OR selfdriveStateSP.mads.enabled)
  i.e. openpilot (or MADS) is actively controlling. Requires onroad. Read live via a per-thread SubMaster;
  guarded and defaults False (offroad, or no messages yet). This is the gate the device's own Device panel
  uses for Reboot / Power Off / Reset Calibration ("Disengage to …")."""
  if not is_onroad(params):
    return False
  try:
    sm = getattr(_tls, "sm", None)
    if sm is None:
      import cereal.messaging as messaging
      sm = messaging.SubMaster(["selfdriveState", "selfdriveStateSP"])
      _tls.sm = sm
    sm.update(0)
    return bool(sm["selfdriveState"].enabled or sm["selfdriveStateSP"].mads.enabled)
  except Exception:
    return False


class _Handler(BaseHTTPRequestHandler):
  server_version = "sunnyconf/1"
  protocol_version = "HTTP/1.1"
  params: Params = None  # set on the server instance

  # -- helpers ---------------------------------------------------------------
  def log_message(self, fmt, *args):
    cloudlog.debug("sunnyconf.http " + (fmt % args))

  def _send_file(self, path, content_type: str):
    """Stream a file with HTTP Range support (206) — required for seekable MP4 playback in MediaPlayer."""
    try:
      size = path.stat().st_size
    except OSError:
      return self._send(404, {"error": "not_found"})
    start, end = 0, size - 1
    rng = self.headers.get("Range")
    partial = False
    if rng and rng.startswith("bytes="):
      try:
        spec = rng[len("bytes="):].split(",")[0].strip()
        s, _, e = spec.partition("-")
        if s:
          start = int(s)
          end = int(e) if e else size - 1
        elif e:               # suffix range: last N bytes
          start = max(0, size - int(e))
        end = min(end, size - 1)
        # ANY parsed Range gets a 206 + Content-Range, even "bytes=0-" that covers the whole file: old
        # MediaPlayer (KitKat head unit) treats a 200 answer to its Range probe as "this source can't be
        # seeked" and from then on restarts the segment from 0 instead of seeking.
        partial = True
        if start > end:
          self.send_response(416)
          self.send_header("Content-Range", f"bytes */{size}")
          self.end_headers()
          return
      except ValueError:
        start, end, partial = 0, size - 1, False
    length = end - start + 1
    self.send_response(206 if partial else 200)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(length))
    self.send_header("Accept-Ranges", "bytes")
    if partial:
      self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    self.send_header("Access-Control-Allow-Origin", "*")
    self.end_headers()
    try:
      with open(path, "rb") as f:
        f.seek(start)
        left = length
        while left > 0:
          chunk = f.read(min(65536, left))
          if not chunk:
            break
          self.wfile.write(chunk)
          left -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
      pass   # player dropped the connection mid-stream (seek) — normal

  def _send_raw(self, code: int, content_type: str, body: bytes):
    self.send_response(code)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    if body:
      self.wfile.write(body)

  def _send(self, code: int, obj):
    body = b"" if obj is None else json.dumps(obj).encode("utf-8")
    self.send_response(code)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, DELETE, OPTIONS")
    self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    if body:
      self.wfile.write(body)

  def _body_json(self) -> dict:
    length = int(self.headers.get("Content-Length", 0) or 0)
    if length <= 0:
      return {}
    raw = self.rfile.read(length)
    try:
      data = json.loads(raw.decode("utf-8"))
      return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
      return {}

  @property
  def _params(self) -> Params:
    return type(self).params or self.server.params

  # -- verbs -----------------------------------------------------------------
  def do_OPTIONS(self):
    self._send(204, None)

  @authorized
  def do_GET(self):
    u = urlparse(self.path)
    path = u.path.rstrip("/") or "/"
    q = parse_qs(u.query)
    refresh = q.get("refresh", ["0"])[0] in ("1", "true")
    include_all = q.get("all", ["0"])[0] in ("1", "true")
    try:
      if path == "/health":
        return self._send(200, {"ok": True})
      if path == "/status":
        return self._send(200, self._status())
      if path == "/schema":
        sc = schema_gen.build_schema(refresh=refresh, include_all=include_all)
        # attach live device capabilities (from CarParams) so the client can evaluate visibility/enablement
        # rules exactly like the sunnylink web app — reuses sunnypilot's own capability builder.
        try:
          from openpilot.sunnypilot.sunnylink.capabilities import generate_capabilities, CAPABILITY_LABELS
          sc = dict(sc)
          sc["capabilities"] = generate_capabilities(self._params)
          sc["capability_labels"] = CAPABILITY_LABELS   # field -> human label (e.g. torque_allowed) for disabled reasons
        except Exception:
          cloudlog.exception("sunnyconf: capabilities unavailable")
        return self._send(200, sc)
      if path == "/values":
        return self._send(200, self._values(include_all))
      if path == "/backup":
        return self._backup()
      if path == "/maps":
        return self._send(200, self._maps())
      if path == "/maps/check":
        return self._send(200, self._maps_check())
      if path == "/drives":
        progress = drives.ensure_indexing()   # make sure the watcher-indexer is alive
        return self._send(200, {"drives": drives.route_summaries(), "indexing": progress})
      if path == "/drives/live":
        onroad = is_onroad(self._params)
        live = drives.live_status(onroad)
        live["onroad"] = onroad
        return self._send(200, live)
      if path.startswith("/drives/"):
        return self._drives_sub(path[len("/drives/"):], q)
      if path == "/clients":
        return self._send(200, {"clients": auth.list_clients()})
      if path.startswith("/params/"):
        return self._get_param(unquote(path[len("/params/"):]))
      return self._send(404, {"error": "not_found"})
    except Exception:
      cloudlog.exception("sunnyconf.http.get")
      return self._send(500, {"error": "internal"})

  @authorized
  def do_PUT(self):
    u = urlparse(self.path)
    path = u.path.rstrip("/") or "/"
    try:
      if path == "/maps":
        return self._put_maps()
      if path.startswith("/params/"):
        return self._put_param(unquote(path[len("/params/"):]))
      return self._send(404, {"error": "not_found"})
    except Exception:
      cloudlog.exception("sunnyconf.http.put")
      return self._send(500, {"error": "internal"})

  @authorized
  @authorized
  def do_POST(self):
    u = urlparse(self.path)
    path = u.path.rstrip("/") or "/"
    try:
      if path == "/pair":            # in _OPEN_PATHS — guarded by the PIN, not a token
        return self._pair()
      m = re.match(r"^/drives/([0-9a-f]{8}--[0-9a-f]{10})/clip$", path)
      if m:
        return self._make_clip(m.group(1), self._body_json())
      if path == "/restore":
        return self._restore()
      if path.startswith("/actions/"):
        return self._action(unquote(path[len("/actions/"):]))
      return self._send(404, {"error": "not_found"})
    except Exception:
      cloudlog.exception("sunnyconf.http.post")
      return self._send(500, {"error": "internal"})

  @authorized
  def do_DELETE(self):
    u = urlparse(self.path)
    path = u.path.rstrip("/") or "/"
    try:
      if path.startswith("/clients/"):
        cid = unquote(path[len("/clients/"):])
        return self._send(200, {"ok": True, "removed": auth.revoke(cid), "client_id": cid})
      if path.startswith("/params/"):
        return self._delete_param(unquote(path[len("/params/"):]))
      if path.startswith("/maps/"):
        return self._delete_country(unquote(path[len("/maps/"):]))
      m = re.match(r"^/drives/([0-9a-f]{8}--[0-9a-f]{10})$", path)
      if m:
        res = drives.delete_route(m.group(1))
        return self._send(200 if res.get("ok") else 409, res)
      return self._send(404, {"error": "not_found"})
    except Exception:
      cloudlog.exception("sunnyconf.http.delete")
      return self._send(500, {"error": "internal"})

  # -- endpoint impls --------------------------------------------------------
  def _action(self, name: str):
    """Run a device action. Each branch is the exact param work the on-device button does — see ACTIONS."""
    # Drain the request body first. We don't need it, but leaving it unread desyncs the keep-alive stream:
    # the client reuses the connection and the next request gets parsed from the leftover bytes, which the
    # base handler answers with a bogus 501/404. _put_param avoids this only because it reads the body.
    try:
      n = int(self.headers.get("Content-Length") or 0)
      if n > 0:
        self.rfile.read(n)
    except (TypeError, ValueError):
      pass

    spec = ACTIONS.get(name)
    if spec is None:
      return self._send(404, {"error": "unknown_action"})
    params = self._params          # a property, not a method
    gate = spec["gate"]
    if gate == "engaged" and is_engaged(params):
      return self._send(409, {"error": "engaged"})       # "Disengage to …"
    if gate == "offroad" and is_onroad(params):
      return self._send(409, {"error": "offroad_only"})

    if name == "reboot":
      params.put_bool("DoReboot", True)
    elif name == "poweroff":
      params.put_bool("DoShutdown", True)
    elif name == "delete_maps":
      # stock _do_delete_maps: wipe the offline tree, then clear the selection/download params
      try:
        if bool(_mem_params().get("OSMDownloadLocations")):
          return self._send(409, {"error": "downloading"})
      except Exception:
        pass
      if MAP_PATH.exists():
        shutil.rmtree(MAP_PATH, ignore_errors=True)
      for k in _OSM_RESET_KEYS:
        params.remove(k)
      _save_osm_countries([])   # also clear the multi-country selection
      global _maps_size_cache
      _maps_size_cache = (0.0, 0, {})
    elif name == "osm_update":
      try:
        if bool(_mem_params().get("OSMDownloadLocations")):
          return self._send(409, {"error": "downloading"})
      except Exception:
        pass
      nations, states = _osm_selection(params)
      if not nations:
        return self._send(400, {"error": "no_country"})
      # inline of mapd_manager.request_refresh_osm_location_data (importing the manager would drag in its
      # heavy live-map deps): stamp the check time, clear the device-panel pending flag, hand mapd the list.
      params.put("OsmDownloadedDate", str(time.time()))
      params.put_bool("OsmDbUpdatesCheck", False)
      _mem_params().put("OSMDownloadLocations", {"nations": nations, "states": states})
      cloudlog.info(f"sunnyconf: osm_update nations={nations} states={states}")
    elif name == "reset_calibration":
      for k in _CALIBRATION_KEYS:
        params.remove(k)
      params.put_bool("OnroadCycleRequested", True)
    elif name == "reset_settings":
      for k in params.all_keys():
        params.remove(k)
      params.put_bool("DoReboot", True)   # device UI calls HARDWARE.reboot(); the manager honours this too

    cloudlog.warning(f"sunnyconf.action: {name}")
    return self._send(200, {"ok": True, "action": name})

  def _pair(self):
    """Verify the device pairing code and register the client, returning its long-lived token."""
    ip = self.client_address[0] if self.client_address else "?"
    wait = auth.rate_limited(ip)
    if wait > 0:
      return self._send(429, {"error": "rate_limited", "retry_after": wait})
    code = self._params.get(PAIRING_CODE_KEY)   # STRING param -> str or None (this fork's get() has no encoding kw)
    if not auth.code_set(code):
      # user hasn't chosen a pairing code on the device yet — pairing is disabled until they do
      return self._send(403, {"error": "pairing_disabled"})
    body = self._body_json()
    if not auth.pin_ok(body.get("pin"), code):
      auth.record_fail(ip)
      cloudlog.info(f"sunnyconf: pairing rejected (bad code) from {ip}")
      return self._send(401, {"error": "bad_code"})
    auth.clear_fails(ip)
    rec = auth.pair(body.get("client_id"), body.get("label"))
    cloudlog.info(f"sunnyconf: paired client {rec['client_id']} ({rec['label']!r})")
    return self._send(200, {
      "ok": True,
      "token": rec["token"],
      "client_id": rec["client_id"],
      "label": rec["label"],
      "paired_at": rec["paired_at"],
      "device": device.instance_name(self._params),
    })

  def _status(self) -> dict:
    p = self._params
    onroad = is_onroad(p)
    sp = schema_gen.build_schema()["sunnypilot"]
    info = device.device_info(p)
    return {
      "onroad": onroad,
      "offroad": not onroad,
      "engaged": is_engaged(p),             # openpilot/MADS actively controlling — gates the Device actions
      "name": device.instance_name(p),      # matches the mDNS instance, e.g. comma3x-1c5bce9a
      "model": info["model"],               # comma3 / comma3x / comma4 (auto-detected)
      "device_type": info["device_type"],   # tici / tizi / mici
      "dongle_id": info["dongle_id"],       # comma Dongle ID
      "sunnylink_id": info["sunnylink_id"], # sunnylink Device ID
      "serial": info["serial"],
      "version": info["version"],           # software version string (Home card + status pill)
      "schema_version": SCHEMA_VERSION,
      "daemon_version": DAEMON_VERSION,     # the app warns when this is older than its features need
      "sunnypilot_commit": sp["commit"],
      "branch": sp["branch"],
    }

  def _values(self, include_all: bool) -> dict:
    p = self._params
    idx = schema_gen.index_by_key(include_all=include_all)
    return {key: values.current_transport(p, key, ie.type_token) for key, ie in idx.items()}

  # ── backup / restore ──────────────────────────────────────────────────────────────────────────────

  def _backup(self):
    """One-file settings backup: every schema param value + the custom-page state (car, model, favourites)
    + the offline-maps selection. POST /restore pushes each piece back through the same write paths the
    UI uses, so map/model downloads restart in the background."""
    st = self._status()
    # Only what /restore would actually write: user-visible schema params of writable types. include_all
    # would drag in runtime/diagnostic params (caches, CarParams blobs, ping times) — megabytes of noise
    # that restore rejects anyway.
    idx = schema_gen.index_by_key(include_all=False)
    vals = {k: values.current_transport(self._params, k, ie.type_token) for k, ie in idx.items()
            if ie.type_token in _WRITABLE_TYPES
            and k not in SECRET_PARAMS and k not in BLOCKED_PARAMS and k not in EXTRA_KEYS}
    return self._send(200, {
      "sunnyconf_backup": 1,
      "created": int(time.time()),
      "device": {k: st.get(k) for k in ("name", "dongle_id", "serial", "model", "device_type", "version",
                                        "sunnypilot_commit", "schema_version")},
      "values": vals,
      "extras": {
        **{k: values.current_transport(self._params, k, EXTRA_KEYS[k]["type"])
           for k in ("CarPlatformBundle", "ModelManager_ActiveBundle", "ModelManager_Favs")},
        # sunnylink identity: the GitHub pairing hangs off this id + the device key in /persist (which
        # survives even a full reset) — restoring the id restores the pairing. Guarded by serial on restore.
        "SunnylinkDongleId": self._params.get("SunnylinkDongleId") or "",
        # SSH access: the enable toggle is a schema value, but the authorized keys + username live here so
        # SSH works immediately after a restore (these are PUBLIC keys — safe to carry in the backup file)
        "GithubUsername": self._params.get("GithubUsername") or "",
        "GithubSshKeys": self._params.get("GithubSshKeys") or "",
      },
      "maps": {
        "countries": _load_osm_countries(),
        "state_name": self._params.get("OsmStateName") or "",
        "state_title": self._params.get("OsmStateTitle") or "",
      },
    })

  def _restore(self):
    """Apply a /backup file. Offroad only. Values go through the same validation as PUT /params/<key>;
    unknown keys (another fork/version) are skipped and reported, never written blind. Car selection and
    favourites are written raw; the model is re-downloaded by ref via ModelManager_DownloadIndex; the map
    selection is saved and the mapd download cycle is kicked — both continue in the background."""
    if is_onroad(self._params):
      return self._send(409, {"error": "offroad_required"})
    body = self._body_json()
    if body.get("sunnyconf_backup") != 1:
      return self._send(400, {"error": "not_a_backup"})
    vals = body.get("values") or {}
    if not isinstance(vals, dict):
      return self._send(400, {"error": "bad_values"})

    idx = schema_gen.index_by_key(include_all=True)
    applied, unknown, invalid, skipped = 0, [], {}, []
    for key in sorted(vals):
      if key in SECRET_PARAMS or key in BLOCKED_PARAMS or key in EXTRA_KEYS:
        skipped.append(key)   # secrets/trust params never restore; extras go through their own path below
        continue
      ie = idx.get(key)
      if ie is None:
        unknown.append(key)
        continue
      if ie.type_token not in _WRITABLE_TYPES:
        skipped.append(key)
        continue
      try:
        self._params.put(key, values.parse_incoming(ie.entry, ie.type_token, vals[key]))
        applied += 1
      except Exception as e:   # one bad value must not stop the rest
        invalid[key] = str(e)

    extras = body.get("extras") or {}
    extras_written = []
    for key in ("CarPlatformBundle", "ModelManager_Favs"):
      raw = extras.get(key)
      if raw in (None, "", "{}"):
        continue
      try:
        self._params.put(key, _extra_coerce(EXTRA_KEYS[key]["type"], raw))
        extras_written.append(key)
      except Exception as e:
        invalid[key] = str(e)
    # SSH: keys + username go back verbatim, so an SshEnabled=1 from `values` is immediately usable.
    # DELIBERATE exception to BLOCKED_PARAMS (these grant SSH trust): a restore is a paired client replaying
    # a whole backup after an explicit on-screen confirmation that names the SSH keys — unlike a single
    # blind PUT, which stays refused. The keys in the file are public keys the user backed up himself.
    for key in ("GithubUsername", "GithubSshKeys"):
      raw = extras.get(key)
      if raw:
        try:
          self._params.put(key, str(raw))
          extras_written.append(key)
        except Exception as e:
          invalid[key] = str(e)

    report = {
      "ok": True, "applied": applied, "extras": extras_written,
      "unknown": unknown, "invalid": invalid, "skipped": skipped,
      "model": self._restore_model(extras.get("ModelManager_ActiveBundle")),
      "maps": self._restore_maps(body.get("maps") or {}),
      "sunnylink": self._restore_sunnylink_id(body, extras.get("SunnylinkDongleId")),
    }
    cloudlog.warning(f"sunnyconf.restore: applied={applied} unknown={len(unknown)} invalid={len(invalid)} "
                     f"model={report['model']} maps={report['maps']}")
    return self._send(200, report)

  def _restore_model(self, active_raw) -> str:
    """Re-select the backed-up model by matching it against the current manifest and writing
    ModelManager_DownloadIndex — the exact write the Models page does, so the manager downloads and
    activates it in the background. Indexes shift between manifest versions, so match by ref/name."""
    if not active_raw:
      return "none"
    try:
      want = json.loads(active_raw) if isinstance(active_raw, str) else dict(active_raw)
    except Exception:
      return "unreadable"
    if not want:
      return "none"   # Default model — nothing to download
    try:
      raw = self._params.get("ModelManager_ModelsCache")   # JSON-typed: this fork's get() returns a dict
      man = raw if isinstance(raw, dict) else json.loads(raw or "")
      bundles = man.get("bundles") or []
    except Exception:
      bundles = []
    if not bundles:
      # no manifest yet (fresh install / offline) — ask the manager to refetch it; re-run restore (or pick
      # the model by hand) once the device has internet
      try:
        self._params.put("ModelManager_LastSyncTime", 0)
      except Exception:
        pass
      return "no_manifest"
    # ActiveBundle is written camelCase (internalName/displayName) while the manifest bundles are
    # snake_case (ref/short_name/display_name) — compare every name either side carries.
    name_keys = ("ref", "internalName", "internal_name", "shortName", "short_name", "displayName", "display_name")
    want_names = {str(want[k]).strip() for k in name_keys if want.get(k)}
    if not want_names:
      return "unmatchable"
    for b in bundles:
      b_names = {str(b[k]).strip() for k in name_keys if b.get(k)}
      if want_names & b_names:
        try:
          self._params.put("ModelManager_DownloadIndex", int(b.get("index")))
        except Exception as e:
          return f"write_failed: {e}"
        return "downloading:" + str(b.get("display_name") or b.get("short_name") or next(iter(b_names)))
    return "not_in_manifest"

  def _restore_sunnylink_id(self, body: dict, backed_id) -> str:
    """Bring the sunnylink (GitHub) pairing back after a full reset. The pairing hangs off SunnylinkDongleId
    plus the device key in /persist — the key survives everything, so writing the id back restores the
    pairing. Guarded by hardware serial: a backup from a DIFFERENT device must not steal an identity the
    server ties to another key. No-op when the device is already registered with the same id."""
    if not backed_id or backed_id == "UnregisteredDevice":
      return "none"
    cur = self._params.get("SunnylinkDongleId") or ""
    if cur == backed_id:
      return "unchanged"
    if cur and cur != "UnregisteredDevice":
      # the device ALREADY re-registered under a new id — the cloud issued fresh credentials for it, and
      # forcing the old id back causes an auth mismatch that really breaks the link (verified 2026-07-13).
      # Restore the id only into the pre-registration window; after that, claim the new id in the dashboard.
      return "skipped_already_registered"
    dev = body.get("device") or {}
    backed_serial = (dev.get("serial") or "").strip()
    cur_serial = (self._params.get("HardwareSerial") or "").strip()
    if backed_serial and cur_serial and backed_serial != cur_serial:
      return "skipped_different_device"
    self._params.put("SunnylinkDongleId", str(backed_id))
    return "restored"

  def _restore_maps(self, m: dict) -> str:
    """Restore the offline-maps selection and kick the download — the same writes as PUT /maps followed
    by POST /actions/osm_update."""
    raw = m.get("countries") or []
    countries = [{"ref": str(c["ref"]).strip(), "title": str(c.get("title") or c["ref"])}
                 for c in raw if isinstance(c, dict) and (c.get("ref") or "").strip()]
    if not countries:
      return "none"
    _save_osm_countries(countries)
    _mirror_selection(self._params, countries)
    # US state selection: _mirror_selection keeps it only while US is selected; write it back after the mirror
    if any(c["ref"] == "US" for c in countries) and (m.get("state_name") or "").strip():
      self._params.put("OsmStateName", str(m["state_name"]))
      self._params.put("OsmStateTitle", str(m.get("state_title") or m["state_name"]))
    try:
      if bool(_mem_params().get("OSMDownloadLocations")):
        return "selected_download_busy"   # selection saved; the running download keeps its own list
    except Exception:
      pass
    nations, states = _osm_selection(self._params)
    self._params.put("OsmDownloadedDate", str(time.time()))
    self._params.put_bool("OsmDbUpdatesCheck", False)
    _mem_params().put("OSMDownloadLocations", {"nations": nations, "states": states})
    return "downloading"

  def _maps(self) -> dict:
    """Everything the app's Maps (OSM) page needs in one poll — mirrors what the stock panel's 1s
    _update_labels() reads: params + mapd's live download state from the mem-params root."""
    p = self._params
    downloading = False
    try:
      downloading = bool(_mem_params().get("OSMDownloadLocations"))   # non-empty == download in flight
    except Exception:
      pass
    progress = None
    try:
      raw = p.get("OSMDownloadProgress")   # JSON param: dict in this fork's Python API, but be liberal
      progress = raw if isinstance(raw, dict) else (json.loads(raw) if raw else None)
    except Exception:
      progress = None
    countries = _load_osm_countries()
    total, excl = _maps_inventory(countries)
    return {
      "ok": True,
      "version": p.get("MapdVersion") or "",
      "size_bytes": total,
      "downloading": downloading,
      "pending": bool(p.get_bool("OsmDbUpdatesCheck")),      # CHECK pressed, manager hasn't started yet
      "progress": progress,                                  # {"total_files": N, "downloaded_files": M} | null
      "downloaded_date": p.get("OsmDownloadedDate") or "",   # unix ts string ("Last checked …")
      "location_name": p.get("OsmLocationName") or "",
      "location_title": p.get("OsmLocationTitle") or "",
      "state_name": p.get("OsmStateName") or "",
      "state_title": p.get("OsmStateTitle") or "",
      # selection [{"ref","title","size_bytes"},...] — size_bytes = that country's EXCLUSIVE tiles (what its
      # ✕ would free; overlapping-box tiles are shared, so this is less than the country's full coverage)
      "countries": [dict(c, size_bytes=excl.get(c["ref"])) for c in countries],
      # bytes owned by MORE than one selected country (overlap zones — e.g. most of KG sits inside KZ's box);
      # kept whichever single country is removed. countries' size_bytes + shared_bytes = size_bytes (total).
      "shared_bytes": max(0, total - sum(excl.values())) if excl else 0,
    }

  def _drives_sub(self, rest: str, q: dict):
    """/drives/<route>/track | thumb/<seg> | playlist.m3u8[?cam=] | qcam/<seg>.ts | cam/<cam>/<seg>.ts"""
    parts = rest.split("/")
    route = parts[0]
    if not re.match(r"^[0-9a-f]{8}--[0-9a-f]{10}$", route):
      return self._send(404, {"error": "bad_route"})
    sub = parts[1] if len(parts) > 1 else ""
    if sub == "track":
      tr = drives.route_track(route)
      return self._send(200, tr) if tr else self._send(404, {"error": "not_indexed", "route": route})
    if sub == "thumb" and len(parts) > 2 and parts[2].isdigit():
      p = drives.thumb_path(route, int(parts[2]))
      if p.exists():
        return self._send_raw(200, "image/jpeg", p.read_bytes())
      return self._send(404, {"error": "no_thumb"})
    if sub == "playlist.m3u8":
      cam = q.get("cam", ["q"])[0]
      pl = drives.playlist_m3u8(route, cam)
      if pl is None:
        return self._send(404, {"error": "no_video"})
      return self._send_raw(200, "application/vnd.apple.mpegurl", pl.encode("utf-8"))
    if sub == "qcam" and len(parts) > 2 and parts[2].endswith(".ts"):
      seg = parts[2][:-3]
      if seg.isdigit():
        p = drives.qcam_path(route, int(seg))
        if p.exists():
          return self._send_raw(200, "video/mp2t", p.read_bytes())
      return self._send(404, {"error": "no_segment"})
    if sub == "video" and len(parts) > 3 and parts[3].endswith(".mp4"):
      # per-segment seekable MP4 (the detail player's format for all four cameras)
      seg = parts[3][:-4]
      if seg.isdigit():
        p = drives.remux_mp4(route, int(seg), parts[2])
        if p is not None:
          drives.prefetch_next(route, int(seg), parts[2])   # warm the next minute
          return self._send_file(p, "video/mp4")
      return self._send(404, {"error": "no_segment"})
    if sub == "clip" and len(parts) > 3 and parts[3].endswith(".mp4"):
      # download a previously-built (or build-on-demand) clip: /clip/<cam>/<start_ms>-<end_ms>.mp4
      cam = parts[2]
      m = re.match(r"^(\d+)-(\d+)$", parts[3][:-4])
      if m and cam in drives._CAM_FILES:
        p = drives.build_clip(route, cam, int(m.group(1)), int(m.group(2)))
        if p is not None:
          return self._send_file(p, "video/mp4")
      return self._send(404, {"error": "clip_failed"})
    return self._send(404, {"error": "not_found"})

  def _make_clip(self, route: str, body: dict):
    """Build an [start_ms,end_ms] slice of the drive (any camera) and return where to download it."""
    cam = str(body.get("cam", "q"))
    if cam not in drives._CAM_FILES:
      return self._send(400, {"error": "bad_cam"})
    try:
      start_ms, end_ms = int(body.get("start_ms")), int(body.get("end_ms"))
    except (TypeError, ValueError):
      return self._send(400, {"error": "bad_range"})
    start_ms = max(0, start_ms)
    if end_ms - start_ms > drives.MAX_CLIP_MS:
      end_ms = start_ms + drives.MAX_CLIP_MS   # clamp so the url/filename match what build_clip produces
    p = drives.build_clip(route, cam, start_ms, end_ms)
    if p is None:
      return self._send(404, {"error": "clip_failed"})
    meta = drives.clip_meta(route, cam, start_ms, end_ms)
    return self._send(200, {"ok": True,
                            "url": f"/drives/{route}/clip/{cam}/{start_ms}-{end_ms}.mp4",
                            "filename": meta["filename"], "bytes": p.stat().st_size,
                            "start_ms": start_ms, "end_ms": end_ms})

  def _maps_check(self) -> dict:
    """Is the map-data server's dataset newer than what we downloaded? (The stock CHECK re-downloads blindly;
    the app asks this first and only offers the download when it would fetch something newer.)
      update_available: true  -> server newer than our download (or nothing downloaded yet)
                        false -> our download is current
                        null  -> couldn't tell (no local tiles to sample / server unreachable)"""
    p = self._params
    try:
      downloaded = float(p.get("OsmDownloadedDate") or 0)
    except (TypeError, ValueError):
      downloaded = 0.0
    if downloaded <= 0 or _maps_size() == 0:
      return {"ok": True, "update_available": True, "remote_date": None, "downloaded_date": downloaded or None,
              "sampled": 0}
    remote, sampled = _osm_remote_date()
    available = None if remote is None else (remote > downloaded)
    return {"ok": True, "update_available": available, "remote_date": remote, "downloaded_date": downloaded,
            "sampled": sampled}

  def _put_maps(self):
    """Set the country selection (multi): body {"countries": [{"ref","title"},...]}. Atomic generalization of
    the stock picker's writes — persist the list, mirror the FIRST country into OsmLocationName/Title +
    OsmLocal (so the on-device panel stays coherent), clear the state selection when US isn't selected, and
    clear everything when the list is empty (the stock US-cancel path)."""
    body = self._body_json()
    raw = body.get("countries")
    if not isinstance(raw, list):
      return self._send(400, {"error": "missing_countries"})
    countries = []
    for c in raw:
      if isinstance(c, dict) and (c.get("ref") or "").strip():
        countries.append({"ref": str(c["ref"]).strip(), "title": str(c.get("title") or c["ref"])})
    _save_osm_countries(countries)
    _mirror_selection(self._params, countries)
    cloudlog.info(f"sunnyconf: osm countries = {[c['ref'] for c in countries]}")
    return self._send(200, {"ok": True, "countries": countries})

  def _delete_country(self, ref: str):
    """Remove ONE country from the selection and delete its offline tiles. Geometry-aware: only tiles inside
    the removed country's bounding box that no remaining country's box covers are deleted (boxes overlap —
    e.g. Kyrgyzstan sits inside Kazakhstan's). Falls back to wipe-and-redownload when the box list can't be
    fetched. 409 while a download runs."""
    try:
      if bool(_mem_params().get("OSMDownloadLocations")):
        return self._send(409, {"error": "downloading"})
    except Exception:
      pass
    countries = _load_osm_countries()
    if not any(c["ref"] == ref for c in countries):
      return self._send(404, {"error": "unknown_country", "ref": ref})
    remaining = [c for c in countries if c["ref"] != ref]

    boxes = _nation_bboxes()
    have_boxes = ref in boxes and all(c["ref"] in boxes for c in remaining)
    redownload = False
    removed_tiles = 0
    if not remaining:
      # last country removed -> same as DELETE all: wipe the tree so no fringe tiles linger
      if MAP_PATH.exists():
        shutil.rmtree(MAP_PATH, ignore_errors=True)
    elif have_boxes:
      gone = _padded(boxes[ref])
      keep = [_padded(boxes[c["ref"]]) for c in remaining]
      stack = [MAP_PATH] if MAP_PATH.exists() else []
      while stack:
        try:
          for entry in os.scandir(stack.pop()):
            if entry.is_dir():
              stack.append(entry.path)
              continue
            tb = _tile_bbox(entry.name)
            if tb is None:
              continue
            if _bbox_intersects(tb, gone) and not any(_bbox_intersects(tb, k) for k in keep):
              try:
                os.unlink(entry.path)
                removed_tiles += 1
              except OSError:
                pass
        except OSError:
          pass
    else:
      # no geometry -> can't tell whose tiles are whose; wipe and re-download what's left
      if MAP_PATH.exists():
        shutil.rmtree(MAP_PATH, ignore_errors=True)
      redownload = bool(remaining)

    _save_osm_countries(remaining)
    _mirror_selection(self._params, remaining)
    if not remaining:
      self._params.remove("OsmDownloadedDate")
    global _maps_size_cache
    _maps_size_cache = (0.0, 0, {})

    if redownload:
      nations, states = _osm_selection(self._params)
      if nations:
        self._params.put_bool("OsmDbUpdatesCheck", False)
        _mem_params().put("OSMDownloadLocations", {"nations": nations, "states": states})
    cloudlog.info(f"sunnyconf: removed country {ref} (tiles={removed_tiles}, redownload={redownload})")
    return self._send(200, {"ok": True, "removed": ref, "countries": remaining,
                            "removed_tiles": removed_tiles, "redownload": redownload})

  def _delete_param(self, key: str):
    """Remove a param — only allowlisted writable EXTRA_KEYS (the stock OSM flow uses params.remove(), and
    remove vs put("") matters: mapd_manager and the update-required alert check key presence)."""
    ex = EXTRA_KEYS.get(key)
    if ex is None:
      return self._send(404, {"error": "unknown_key", "key": key})
    if not ex.get("w"):
      return self._send(403, {"error": "read_only", "key": key})
    if ex.get("offroad") and is_onroad(self._params):
      return self._send(409, {"error": "offroad_required", "key": key})
    self._params.remove(key)
    cloudlog.info(f"sunnyconf: removed {key}")
    return self._send(200, {"ok": True, "key": key, "removed": True})

  def _get_param(self, key: str):
    if key in SECRET_PARAMS:
      return self._send(403, {"error": "secret", "key": key})
    idx = schema_gen.index_by_key(include_all=True)
    ie = idx.get(key)
    if ie is not None:
      return self._send(200, {
        "key": key,
        "value": values.current_transport(self._params, key, ie.type_token),
        "meta": ie.entry,
      })
    ex = EXTRA_KEYS.get(key)
    if ex is not None:  # custom-page params (CarList/ModelManager_*) not in the settings schema
      return self._send(200, {
        "key": key,
        "value": values.current_transport(self._params, key, ex["type"]),
        "meta": {"type": "string", "readonly": not ex["w"]},
      })
    return self._send(404, {"error": "unknown_key", "key": key})

  def _put_param(self, key: str):
    if key in EXTRA_KEYS:
      # car/model custom-page params: write the raw JSON string. These are often JSON-typed params (e.g.
      # CarPlatformBundle) which the schema path rejects as unsupported_type — and some are also in the
      # schema index, so this must win over the idx lookup below.
      return self._put_extra(key)
    idx = schema_gen.index_by_key(include_all=True)
    ie = idx.get(key)
    if ie is None:
      return self._put_extra(key)   # any other non-schema key -> 404 unless allowlisted
    if key in BLOCKED_PARAMS:
      # only the trust-granting params — the SDUI's broader `blocked` ("Device only" in the cloud) is writable
      # here: local pairing (PIN off the device's screen) is as good as standing at the device (see __init__.py)
      return self._send(403, {"error": "blocked", "key": key})
    if ie.type_token not in _WRITABLE_TYPES:
      return self._send(400, {"error": "unsupported_type", "key": key, "type": ie.type_token})
    if ie.offroad_only and is_onroad(self._params):
      return self._send(409, {"error": "offroad_required", "key": key})

    body = self._body_json()
    if "value" not in body:
      return self._send(400, {"error": "missing_value", "key": key})
    try:
      typed = values.parse_incoming(ie.entry, ie.type_token, body["value"])
    except (ValueError, TypeError) as e:
      return self._send(400, {"error": "invalid_value", "key": key, "detail": str(e)})

    try:
      self._params.put(key, typed)
    except Exception as e:
      cloudlog.exception(f"sunnyconf.put.{key}")
      return self._send(500, {"error": "write_failed", "key": key, "detail": str(e)})

    cloudlog.info(f"sunnyconf: set {key} = {typed!r}")
    return self._send(200, {
      "ok": True,
      "key": key,
      "value": values.current_transport(self._params, key, ie.type_token),
      "requires_restart": ie.entry.get("requires_restart", False),
    })

  def _put_extra(self, key: str):
    """Write a custom-page param (CarPlatformBundle / ModelManager_*) from the EXTRA_KEYS allowlist.
    These aren't in the SDUI schema, so they bypass parse_incoming — the value is written as a raw
    string (JSON). Only allowlisted-writable keys are accepted; onroad writes to offroad keys are 409."""
    ex = EXTRA_KEYS.get(key)
    if ex is None:
      return self._send(404, {"error": "unknown_key", "key": key})
    if not ex.get("w"):
      return self._send(403, {"error": "read_only", "key": key})
    if ex.get("offroad") and is_onroad(self._params):
      return self._send(409, {"error": "offroad_required", "key": key})
    body = self._body_json()
    if "value" not in body:
      return self._send(400, {"error": "missing_value", "key": key})
    # Params.put type-checks the value against the param's declared type — see _extra_coerce.
    try:
      val = _extra_coerce(ex.get("type"), body["value"])
    except ValueError:
      return self._send(400, {"error": "not_an_int", "key": key, "value": body["value"]})
    try:
      self._params.put(key, val)
    except Exception as e:
      cloudlog.exception(f"sunnyconf.put.{key}")
      return self._send(500, {"error": "write_failed", "key": key, "detail": str(e)})
    cloudlog.info(f"sunnyconf: set {key} (extra) type={type(val).__name__}")
    return self._send(200, {"ok": True, "key": key, "value": val})


def make_server(port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
  httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
  httpd.daemon_threads = True
  httpd.params = Params()
  return httpd


