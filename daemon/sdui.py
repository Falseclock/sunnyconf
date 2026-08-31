"""
sunnyconf.sdui — reuse of the sunnylink SDUI (Settings-Driven UI) definition.

sunnypilot deprecated the flat params_metadata.json (#1862) in favour of a full declarative settings
schema: sunnypilot/sunnylink/settings_ui.json (compiled from settings_ui_src/ by tools/compile_settings_ui.py).
The sunnylink cloud renders settings from it. sunnyconf reuses the SAME file as its primary schema source, so
the field menu matches the cloud and inherits upstream additions with no code change.

The SDUI gives us, per param, what our raylib-layout AST only approximated: the panel/group (with icon and
order), a curated title/description, the widget kind, the real stored-value domain (option min/max/step,
multiple_button option values), and — crucially — declarative offroad gating via enablement rules. We flatten
panels → sections → sub_panels (and per-brand vehicle_settings) into a flat key -> SduiItem index.

Structure (verified): {panels:[{id,label,icon,order,description, sections:[{id,title,description,
items:[item], sub_panels:[{id,label,trigger_key,trigger_condition, items:[item]}]}]}], vehicle_settings:
{<brand>:{title,description, items:[item]}}}. item = {key, widget(toggle|multiple_button|option|info),
title, description?, options?:[{value,label,enablement?}], min?, max?, step?, unit?, enablement?, visibility?,
title_param_suffix?}. enablement/visibility rule types: offroad_only, param, param_compare, capability, not,
any, all, not_engaged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache

from .registry import repo_root

# SDUI widget -> our base widget/type hint (registry decides int vs float for 'option')
WIDGET_TYPE = {"toggle": "bool", "multiple_button": "enum", "option": "number", "info": "info"}


@dataclass
class SduiItem:
  key: str
  group_id: str
  group_title: str
  group_icon: str | None = None
  group_order: int | None = None
  group_desc: str = ""
  section: str = ""
  section_desc: str = ""                                   # the section's own description (web SettingCard subtitle)
  widget: str = ""
  title: str = ""
  description: str = ""
  options: list[list[str]] = field(default_factory=list)   # [[value, label], ...]
  min: float | int | None = None
  max: float | int | None = None
  step: float | int | None = None
  unit: str | None = None
  offroad_only: bool = False
  readonly: bool = False                                   # widget == info
  enablement: list = field(default_factory=list)           # raw rules, passed through to the client
  visible_when: list = field(default_factory=list)         # sub_panel trigger + item visibility
  details: str = ""                                        # extra help text (web InfoDetailsModal ⓘ)
  title_param_suffix: dict | None = None                   # {param, values:{"true":..,"false":..}} dynamic title suffix
  blocked: bool = False                                    # device-only: read-only from a remote client (SSH/ADB)
  sub_panel: dict | None = None                            # {id,label,parent,trigger} if this item lives in a navigable sub-panel
  order: int = 0


def sdui_path():
  return repo_root() / "sunnypilot" / "sunnylink" / "settings_ui.json"


@lru_cache(maxsize=1)
def load_sdui() -> dict:
  try:
    with open(sdui_path(), encoding="utf-8") as f:
      data = json.load(f)
    return data if isinstance(data, dict) else {}
  except (OSError, json.JSONDecodeError):
    return {}


def _has_offroad_only(enablement) -> bool:
  for rule in enablement or []:
    if isinstance(rule, dict) and rule.get("type") == "offroad_only":
      return True
  return False


def _norm_options(opts) -> list[list[str]]:
  out: list[list[str]] = []
  for o in opts or []:
    if isinstance(o, dict) and "value" in o:
      out.append([str(o["value"]), str(o.get("label", o["value"]))])
  return out


class _Counter:
  def __init__(self):
    self.n = 0

  def next(self) -> int:
    self.n += 1
    return self.n


def _make_item(it: dict, gid, gtitle, gicon, gorder, gdesc, section, section_desc, sub_panel, extra_visible, counter) -> SduiItem | None:
  key = it.get("key")
  if not key or not isinstance(key, str):
    return None
  widget = it.get("widget", "")
  enablement = it.get("enablement") or []
  visible = list(extra_visible) + list(it.get("visibility") or [])
  tps = it.get("title_param_suffix")
  return SduiItem(
    key=key, group_id=gid, group_title=gtitle, group_icon=gicon, group_order=gorder, group_desc=gdesc,
    # an item may carry its OWN section (block header) — the way sub_panel items (no native SDUI sections)
    # form titled blocks, e.g. the Cluster TSR Alarm block inside Speed Limit Settings
    section=it.get("section", section) or section, section_desc=it.get("section_description", section_desc) or section_desc,
    widget=widget, title=it.get("title", ""), description=it.get("description", ""),
    options=_norm_options(it.get("options")),
    min=it.get("min"), max=it.get("max"), step=it.get("step"), unit=it.get("unit"),
    offroad_only=_has_offroad_only(enablement), readonly=(widget == "info"),
    enablement=enablement, visible_when=visible,
    details=it.get("details", "") or "", title_param_suffix=tps if isinstance(tps, dict) else None,
    blocked=bool(it.get("blocked", False)), sub_panel=sub_panel,
    order=counter.next(),
  )


def _walk(node: dict, gid, gtitle, gicon, gorder, gdesc, section, section_desc, sub_panel, extra_visible, counter, out: dict):
  for it in (node.get("items") or []):
    item = _make_item(it, gid, gtitle, gicon, gorder, gdesc, section, section_desc, sub_panel, extra_visible, counter)
    if item and item.key not in out:
      out[item.key] = item
    # sub_items are rows nested under a toggle (e.g. BlinkerPauseLateralControl -> Minimum Speed / Post-Blinker
    # Delay). They belong to the SAME section and carry their OWN enablement (typically the parent param ==
    # true). Stock hides them with set_visible; we render them right after the parent as ordinary disabled
    # rows (playbook §3: disable, don't hide). Walked in place so counter/order keeps them under the parent.
    for sub in (it.get("sub_items") or []):
      s = _make_item(sub, gid, gtitle, gicon, gorder, gdesc, section, section_desc, sub_panel, extra_visible, counter)
      if s and s.key not in out:
        out[s.key] = s
  for sec in (node.get("sections") or []):
    _walk(sec, gid, gtitle, gicon, gorder, gdesc, sec.get("title", "") or section, sec.get("description", ""),
          sub_panel, extra_visible, counter, out)
  # sub_panels are NAVIGABLE: stock/web show a "<label> ›" row in the parent section that drills into a sub-page
  # of the sub_panel's items (MADS Settings, Torque Settings, Speed Limit, Custom ACC). We DON'T flatten them
  # inline — each item is tagged with sub_panel meta {id,label,parent,trigger}; schema_gen emits them as a
  # separate `sub_panels[]` per group. The trigger gates the NAV ROW (button), not the items, so it is NOT added
  # to the items' visibility here (they show in the sub-page, gated by their own enablement).
  for sp in (node.get("sub_panels") or []):
    meta = {"id": sp.get("id", ""), "label": sp.get("label", "") or section,
            "parent": section, "parent_desc": section_desc, "trigger": sp.get("trigger_condition")}
    _walk(sp, gid, gtitle, gicon, gorder, gdesc, "", "", meta, extra_visible, counter, out)


@lru_cache(maxsize=1)
def build_sdui_index() -> dict[str, SduiItem]:
  """key -> SduiItem for every param in the SDUI definition (panels + vehicle_settings)."""
  data = load_sdui()
  out: dict[str, SduiItem] = {}
  counter = _Counter()
  for p in data.get("panels", []) or []:
    _walk(p, p.get("id", ""), p.get("label", "") or p.get("id", ""), p.get("icon"),
          p.get("order"), p.get("description", ""), "", "", None, [], counter, out)

  vs = data.get("vehicle_settings") or {}
  if isinstance(vs, dict):
    for brand, spec in vs.items():
      if not isinstance(spec, dict):
        continue
      _walk(spec, "vehicle", "Vehicle", None, None, "", spec.get("title", "") or brand,
            spec.get("description", ""), None, [], counter, out)
  return out


@lru_cache(maxsize=1)
def group_meta() -> dict:
  """group_id -> {title, icon, order, description} from the SDUI panels (+ a synthetic vehicle group)."""
  data = load_sdui()
  out: dict = {}
  for p in data.get("panels", []) or []:
    gid = p.get("id", "")
    if gid:
      out[gid] = {"title": p.get("label") or gid, "icon": p.get("icon"),
                  "order": p.get("order"), "description": p.get("description", "")}
  if data.get("vehicle_settings"):
    out.setdefault("vehicle", {"title": "Vehicle", "icon": None, "order": None, "description": ""})
  return out


def clear_cache():
  load_sdui.cache_clear()
  build_sdui_index.cache_clear()
  group_meta.cache_clear()
