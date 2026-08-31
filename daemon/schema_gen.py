"""
sunnyconf.schema_gen — build the settings schema served to the client.

Sources, in increasing precedence:
  1. registry  (common/params_keys.h via Params)  -> type, default, "is it a real key" (write coercion)
  2. SDUI      (sunnypilot/sunnylink/settings_ui.json, see sunnyconf.sdui) -> group/panel, title,
               description, widget, options, ranges, declarative offroad — the primary source
  3. config/schema_overrides.json                 -> hand fixes + keys not (yet) in the SDUI

The menu (default /schema) is the union of SDUI + override keys. /schema?all=1 additionally exposes every
other registered key in an "Advanced" group, so nothing is unreachable. Rebuilt on demand (?refresh=1) so a
fork update to settings_ui.json changes the menu with no code edit.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, UTC
from functools import lru_cache
from pathlib import Path

from openpilot.common.swaglog import cloudlog
from . import BLOCKED_PARAMS, SCHEMA_VERSION
from . import registry as reg
from . import sdui
from .registry import SCALAR_TYPE, repo_root
from .values import default_transport

# fallback group ordering / titles for groups without an SDUI panel (e.g. sunnylink, advanced)
_GROUP_ORDER = [
  "device", "drives", "network", "sunnylink", "toggles", "software", "models", "steering",
  "cruise", "visuals", "display", "osm", "navigation", "trips", "vehicle",
  "firehose", "developer", "advanced",
]

# Custom (non-schema) pages the native app renders itself, advertised in /schema.groups so the client only
# offers what this daemon actually supports. "maps" = the stock OSM offline-maps panel
# (selfdrive/ui/sunnypilot/layouts/settings/osm.py), backed by GET /maps + the Osm* EXTRA_KEYS in server.py.
# "drives" = the recorded-routes browser (UI first shipped as a mock; the route-listing endpoints follow).
_CUSTOM_GROUPS = [
  # titled "Maps" (user preference) — the stock panel calls the same page "OSM"
  {"id": "osm", "title": "Maps", "icon": "map", "description": "", "custom": "maps",
   "params": [], "sub_panels": []},
  {"id": "drives", "title": "Drives", "icon": "route", "description": "", "custom": "drives",
   "params": [], "sub_panels": []},
]

_ENTRY_KEYS = ("key", "title", "description", "details", "title_param_suffix", "type", "options",
               "min", "max", "step", "unit", "default", "offroad_only", "requires_restart", "auto_detected",
               "section", "section_description", "readonly", "blocked", "enablement", "visible_when")

_CAMEL_1 = re.compile(r'(.)([A-Z][a-z]+)')
_CAMEL_2 = re.compile(r'([a-z0-9])([A-Z])')


def humanize(key: str) -> str:
  """'SmartCruiseControlVision' -> 'Smart Cruise Control Vision' (fallback title for keys with no label)."""
  s = _CAMEL_2.sub(r'\1 \2', _CAMEL_1.sub(r'\1 \2', key.replace("_", " ")))
  return re.sub(r'\s+', ' ', s).strip()


@dataclass
class IndexEntry:
  group_id: str
  type_token: str          # registry token, for Params write coercion
  offroad_only: bool
  entry: dict              # the per-param schema object
  sub_panel: dict | None = None   # {id,label,parent,trigger} if this param lives in a navigable sub-panel


def _overrides_path():
  return Path(__file__).resolve().parent / "config" / "schema_overrides.json"


@lru_cache(maxsize=1)
def _load_overrides() -> dict:
  try:
    with open(_overrides_path(), encoding="utf-8") as f:
      data = json.load(f)
    return data if isinstance(data, dict) else {}
  except (OSError, json.JSONDecodeError):
    return {}


def _norm_options(opts) -> list[list[str]]:
  out: list[list[str]] = []
  for o in opts or []:
    if isinstance(o, dict):
      out.append([str(o.get("value", "")), str(o.get("label", o.get("value", "")))])
    elif isinstance(o, (list, tuple)) and len(o) >= 2:
      out.append([str(o[0]), str(o[1])])
  return out


def _git_info() -> tuple[str, str]:
  try:
    from openpilot.common.params import Params
    p = Params()
    commit = (p.get("GitCommit") or "")[:12]
    branch = p.get("GitBranch") or ""
    if commit or branch:
      return commit or "unknown", branch or "unknown"
  except Exception:
    pass
  try:
    import subprocess
    root = str(repo_root())
    commit = subprocess.check_output(["git", "-C", root, "rev-parse", "--short", "HEAD"], text=True).strip()
    branch = subprocess.check_output(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
    return commit, branch
  except Exception:
    return "unknown", "unknown"


def _number_type(type_token: str, mn, mx, step) -> str:
  if type_token == "FLOAT":
    return "float"
  if type_token == "INT":
    return "int"
  return "float" if any(isinstance(v, float) for v in (mn, mx, step) if v is not None) else "int"


def _entry_from_sdui(key: str, item: sdui.SduiItem, ov: dict | None, type_token: str,
                     default_stored: str | None) -> IndexEntry:
  ov = ov or {}
  options = _norm_options(ov.get("options")) or item.options

  wt = sdui.WIDGET_TYPE.get(item.widget, "string")
  if wt == "number":
    wt = _number_type(type_token, item.min, item.max, item.step)
  elif wt in ("info", "string"):
    wt = SCALAR_TYPE.get(type_token, "string")

  entry = {
    "key": key,
    "title": ov.get("title") or item.title or humanize(key),
    "description": ov["description"] if "description" in ov else (item.description or ""),
    # extra help text (web InfoDetailsModal ⓘ) + dynamic title suffix ({param, values}) — passed to the client
    "details": ov.get("details", item.details) or "",
    "title_param_suffix": ov.get("title_param_suffix", item.title_param_suffix),
    "type": ov.get("type") or wt,
    "options": options,
    "min": ov.get("min", item.min),
    "max": ov.get("max", item.max),
    "step": ov.get("step", item.step),
    "unit": ov.get("unit", item.unit),
    "default": ov.get("default") or default_transport(default_stored, type_token),
    "offroad_only": bool(ov["offroad_only"]) if "offroad_only" in ov else bool(item.offroad_only),
    "requires_restart": bool(ov.get("requires_restart", False)),
    "auto_detected": bool(ov.get("auto_detected", False)),
    "section": ov.get("section", item.section) or "",
    "section_description": ov.get("section_description", item.section_desc) or "",   # web SettingCard subtitle
    "readonly": bool(ov.get("readonly", item.readonly)),
    # "blocked" tells the client a param is not writable through this daemon. The SDUI's own blocked flag
    # ("Device only" in the cloud, e.g. the SSH/ADB toggles) deliberately does NOT carry over: the cloud is
    # reachable from anywhere, our client paired via the PIN on the device's screen over local WiFi — that's
    # device-presence, so those toggles are writable here. Only the trust-granting params stay locked.
    "blocked": key in BLOCKED_PARAMS,
    "enablement": item.enablement,
    "visible_when": item.visible_when,
    "order": item.order,   # SDUI walk order, so the client can order sections (incl. sub-panel-only ones)
  }
  group_id = ov.get("group") or item.group_id or "advanced"
  return IndexEntry(group_id, type_token, entry["offroad_only"], entry, item.sub_panel)


def _entry_from_registry(key: str, ov: dict | None, type_token: str, default_stored: str | None) -> IndexEntry:
  """For keys not in the SDUI: surfaced by an override (its own group) or, with ?all=1, as Advanced."""
  ov = ov or {}
  options = _norm_options(ov.get("options"))
  wtype = ov.get("type") or ("enum" if options else SCALAR_TYPE.get(type_token, "string"))
  entry = {
    "key": key,
    "title": ov.get("title") or humanize(key),
    "description": ov.get("description", ""),
    "type": wtype,
    "options": options,
    "min": ov.get("min"), "max": ov.get("max"), "step": ov.get("step"), "unit": ov.get("unit"),
    "default": ov.get("default") or default_transport(default_stored, type_token),
    "offroad_only": bool(ov.get("offroad_only", False)),
    "requires_restart": bool(ov.get("requires_restart", False)),
    "auto_detected": bool(ov.get("auto_detected", "group" not in ov)),
    "section": ov.get("section", "") or "",
    "readonly": bool(ov.get("readonly", False)),
    "enablement": [], "visible_when": [],
    "order": 10 ** 6,   # registry/override items have no SDUI order; sort them after SDUI-defined sections
  }
  group_id = ov.get("group") or "advanced"
  return IndexEntry(group_id, type_token, entry["offroad_only"], entry)


def _group_title(gid: str, overrides: dict, gmeta: dict) -> str:
  g = overrides.get("groups", {}).get(gid, {})
  if g.get("title"):
    return g["title"]
  if gmeta.get(gid, {}).get("title"):
    return gmeta[gid]["title"]
  return humanize(gid)


def _group_sort_key(gid: str, overrides: dict, gmeta: dict) -> tuple:
  g = overrides.get("groups", {}).get(gid, {})
  if "order" in g:
    return (0, int(g["order"]), gid)
  o = gmeta.get(gid, {}).get("order")
  if o is not None:
    return (0, int(o), gid)
  if gid in _GROUP_ORDER:
    return (1, _GROUP_ORDER.index(gid), gid)
  return (2, 0, gid)


def _assemble(include_all: bool) -> tuple[dict, dict[str, IndexEntry]]:
  registry = reg.load_registry()
  sdui_idx = sdui.build_sdui_index()
  overrides = _load_overrides()
  ov_params = overrides.get("params", {})

  keys: dict[str, None] = {}
  for k in sdui_idx:
    keys[k] = None
  for k, v in ov_params.items():
    if not v.get("hidden"):
      keys.setdefault(k, None)
  if include_all:
    for k in registry:
      keys.setdefault(k, None)

  index: dict[str, IndexEntry] = {}
  unresolved: list[str] = []
  for key in keys:
    ov = ov_params.get(key)
    if ov and ov.get("hidden"):
      continue
    rmeta = registry.get(key)
    if rmeta is None:
      unresolved.append(key)
      cloudlog.warning(f"sunnyconf.schema: '{key}' not in param registry; skipping")
      continue
    s = sdui_idx.get(key)
    if s is not None:
      index[key] = _entry_from_sdui(key, s, ov, rmeta.type, rmeta.default)
    else:
      index[key] = _entry_from_registry(key, ov, rmeta.type, rmeta.default)

  # Split each group's params into the main list and NAVIGABLE sub-panels. A sub_panel item (ie.sub_panel set)
  # goes into groups[gid].sub_panels[sp_id] instead of the flat params, so the app can draw a "<label> ›" row in
  # the parent section that drills into a sub-page. Insertion order is preserved (SDUI order) for both.
  groups: dict[str, list[dict]] = {}
  subpanels: dict[str, dict[str, dict]] = {}   # gid -> {sp_id -> {id,label,section,trigger,params:[]}}
  for ie in index.values():
    if ie.sub_panel:
      sp = ie.sub_panel
      spid = sp.get("id") or sp.get("label") or ""
      bucket = subpanels.setdefault(ie.group_id, {})
      sp_entry = bucket.get(spid)
      if sp_entry is None:
        sp_entry = {"id": spid, "label": sp.get("label", ""), "section": sp.get("parent", ""),
                    "section_description": sp.get("parent_desc", "") or "", "trigger": sp.get("trigger"),
                    "order": ie.entry.get("order", 0), "params": []}
        bucket[spid] = sp_entry
      sp_entry["params"].append(ie.entry)
      sp_entry["order"] = min(sp_entry["order"], ie.entry.get("order", 0))
    else:
      groups.setdefault(ie.group_id, []).append(ie.entry)

  gmeta = sdui.group_meta()
  custom = {g["id"]: g for g in _CUSTOM_GROUPS}
  ordered_gids = sorted(set(groups) | set(subpanels) | set(custom),
                        key=lambda g: _group_sort_key(g, overrides, gmeta))
  commit, branch = _git_info()
  glist = []
  for gid in ordered_gids:
    if gid in custom and gid not in groups and gid not in subpanels:
      glist.append(dict(custom[gid]))   # app-rendered page (e.g. Maps/OSM): no schema params, just the entry
      continue
    glist.append({
      "id": gid,
      "title": _group_title(gid, overrides, gmeta),
      "icon": gmeta.get(gid, {}).get("icon"),
      "description": gmeta.get(gid, {}).get("description", ""),
      # Preserve the SDUI order (panels -> sections -> items). Sorting alphabetically (or offroad-first) here
      # scrambles the section order + membership, so the app's sections no longer match the web. keys[] is
      # already built in SDUI order (see _assemble), so keep it.
      "params": groups.get(gid, []),
      "sub_panels": list(subpanels.get(gid, {}).values()),
    })
  schema = {
    "schema_version": SCHEMA_VERSION,
    "sunnypilot": {"commit": commit, "branch": branch, "generated_at": datetime.now(UTC).isoformat()},
    "groups": glist,
  }
  if unresolved:
    cloudlog.warning(f"sunnyconf.schema: {len(unresolved)} unresolved keys: {unresolved}")
  cloudlog.info(f"sunnyconf.schema: {len(index)} params in {len(ordered_gids)} groups (all={include_all})")
  return schema, index


# cache the two flavors separately
_CACHE: dict[bool, tuple[dict, dict[str, IndexEntry]]] = {}


def build_schema(refresh: bool = False, include_all: bool = False) -> dict:
  if refresh:
    invalidate()
  if include_all not in _CACHE:
    _CACHE[include_all] = _assemble(include_all)
  return _CACHE[include_all][0]


def index_by_key(include_all: bool = True) -> dict[str, IndexEntry]:
  """Flat key -> IndexEntry (defaults to the full key set so writes to any registered key resolve)."""
  if include_all not in _CACHE:
    _CACHE[include_all] = _assemble(include_all)
  return _CACHE[include_all][1]


def invalidate():
  _CACHE.clear()
  _load_overrides.cache_clear()
  reg.load_registry.cache_clear()
  reg._parse_header.cache_clear()
  sdui.clear_cache()
