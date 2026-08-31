"""
sunnyconf.values — read/coerce/validate param values for the wire.

Transport convention (kept deliberately simple for a dumb schema-driven client): every value
crosses the wire as a JSON string. bool -> "true"/"false"; int/float/enum -> the stringified
stored value; string -> itself. Incoming writes are coerced back to the param's registry type and
validated against the schema entry (options / min / max) before hitting Params.put (which itself
enforces the C++ type).
"""
from __future__ import annotations

_TRUE = {"1", "true", "t", "yes", "on"}
_FALSE = {"0", "false", "f", "no", "off", ""}


def _to_bool(raw) -> bool:
  if isinstance(raw, bool):
    return raw
  if isinstance(raw, (int, float)):
    return bool(raw)
  s = str(raw).strip().lower()
  if s in _TRUE:
    return True
  if s in _FALSE:
    return False
  raise ValueError(f"not a boolean: {raw!r}")


def to_transport(stored: str | None, type_token: str) -> str:
  """stored-string form (as in /data/params/d) -> wire string."""
  if stored is None:
    return "false" if type_token == "BOOL" else ""
  if type_token == "BOOL":
    return "true" if stored not in ("0", "", "false") else "false"
  return str(stored)


def current_transport(params, key: str, type_token: str) -> str:
  """Read the live value of a param and render it for the wire."""
  try:
    if type_token == "BOOL":
      return "true" if params.get_bool(key) else "false"
    val = params.get(key)
    if val is None:
      return ""
    if isinstance(val, (dict, list)):
      # JSON-typed params come back from Params.get already parsed (CPP_2_PYTHON[JSON]=json.loads).
      # Re-serialize to valid JSON for the wire (str() would give a single-quoted Python repr).
      import json
      return json.dumps(val)
    if isinstance(val, bytes):
      import base64
      return base64.b64encode(val).decode("ascii")
    return str(val)
  except Exception:
    return ""


def default_transport(meta_default: str | None, type_token: str) -> str:
  return to_transport(meta_default, type_token)


def _option_values(entry: dict) -> list[str] | None:
  opts = entry.get("options")
  if not opts:
    return None
  return [str(o[0]) for o in opts]


def parse_incoming(entry: dict, type_token: str, raw):
  """Validate `raw` against the schema `entry` and return a Python value typed for Params.put.

  Raises ValueError on any validation failure (caller maps to HTTP 400).
  """
  wtype = entry.get("type", "string")

  if wtype == "bool" or type_token == "BOOL":
    b = _to_bool(raw)
    # A "toggle" widget can sit on a 0/1 INT (or FLOAT) param, not just a BOOL — e.g.
    # BlinkerPauseLateralControl is INT in params_keys.h. Params.put enforces the C++ type, so return the
    # value typed to the REGISTRY token, not the widget: writing a Python bool to an INT param raises
    # (TypeError proposed_type=bool expected_type=INT) -> HTTP 500. int(True)=1 / int(False)=0.
    if type_token == "INT":
      return int(b)
    if type_token == "FLOAT":
      return float(b)
    return b

  if wtype == "enum":
    allowed = _option_values(entry)
    if type_token == "FLOAT":
      val = float(raw)
      cmp = str(val)
    elif type_token == "INT":
      val = int(float(raw))   # tolerate "1.0"
      cmp = str(val)
    else:
      val = str(raw)
      cmp = val
    if allowed is not None and cmp not in allowed and str(raw) not in allowed:
      raise ValueError(f"{raw!r} not in options {allowed}")
    return val

  if wtype == "int" or type_token == "INT":
    val = int(float(raw))
    _check_range(entry, val)
    return val

  if wtype == "float" or type_token == "FLOAT":
    val = float(raw)
    _check_range(entry, val)
    return val

  # string / fallthrough
  return str(raw)


def _check_range(entry: dict, val):
  mn, mx = entry.get("min"), entry.get("max")
  if mn is not None and val < mn:
    raise ValueError(f"{val} < min {mn}")
  if mx is not None and val > mx:
    raise ValueError(f"{val} > max {mx}")
