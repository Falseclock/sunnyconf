"""
sunnyconf.registry — typed access to the openpilot/sunnypilot param registry.

The authoritative registry of params (key -> type, flags, default) is the C++ header
common/params_keys.h. At runtime the compiled Params extension exposes the same data via
Params().all_keys()/get_type()/get_default_value(); we prefer that (it evaluates computed
defaults such as cereal enum values). When the extension cannot be imported (e.g. a CI box
without a build) we fall back to parsing the header statically so the schema generator still
works headless.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# ParamKeyType ordinal -> registry type token (verified: common/params.h)
#   STRING=0 BOOL=1 INT=2 FLOAT=3 TIME=4 JSON=5 BYTES=6
_TYPE_BY_ORDINAL = {0: "STRING", 1: "BOOL", 2: "INT", 3: "FLOAT", 4: "TIME", 5: "JSON", 6: "BYTES"}

# registry type token -> schema scalar type
SCALAR_TYPE = {
  "BOOL": "bool", "INT": "int", "FLOAT": "float",
  "STRING": "string", "TIME": "string", "JSON": "string", "BYTES": "string",
}

# {"Key", {FLAG | FLAG, TYPE[, DEFAULT]}},   (trailing // comments tolerated)
_ENTRY_RE = re.compile(
  r'\{\s*"(?P<key>\w+)"\s*,\s*\{\s*' +
  r'(?P<flags>[A-Z0-9_|\s]+?)\s*,\s*' +
  r'(?P<type>STRING|BOOL|INT|FLOAT|TIME|JSON|BYTES)\b' +
  r'(?:\s*,\s*(?P<default>.+?))?\s*\}\s*\}\s*,'
)


@dataclass(frozen=True)
class ParamMeta:
  key: str
  type: str                 # registry token (BOOL/INT/FLOAT/STRING/TIME/JSON/BYTES)
  flags: frozenset[str]
  default: str | None       # stored-string form ("0"/"1"/"15"/...), or None if unknown


def repo_root() -> Path:
  # Walk up to the repo root (the dir that holds common/params_keys.h and sunnypilot/), so this is
  # robust to how deep this module is nested (e.g. sunnyconf/daemon/registry.py) and to being imported
  # through the openpilot/ symlink.
  here = Path(__file__).resolve()
  for parent in here.parents:
    if (parent / "common" / "params_keys.h").exists() and (parent / "sunnypilot").is_dir():
      return parent
  return here.parents[2]


def _params_keys_header() -> Path:
  return repo_root() / "common" / "params_keys.h"


def _strip_default(raw: str | None) -> str | None:
  if raw is None:
    return None
  raw = raw.strip()
  m = re.fullmatch(r'"(.*)"', raw)
  if m:                       # plain quoted literal default
    return m.group(1)
  return None                 # computed C++ default -> leave to runtime refinement


def _stored_str(value, type_token: str) -> str | None:
  """Convert a typed default (from Params.get_default_value) to its stored-string form."""
  if value is None:
    return None
  if type_token == "BOOL":
    return "1" if value else "0"
  if type_token in ("INT", "FLOAT", "STRING"):
    return str(value)
  return None                 # JSON/TIME/BYTES defaults are not user-facing


@lru_cache(maxsize=1)
def _parse_header() -> dict[str, ParamMeta]:
  out: dict[str, ParamMeta] = {}
  try:
    text = _params_keys_header().read_text(encoding="utf-8", errors="replace")
  except OSError:
    return out
  for m in _ENTRY_RE.finditer(text):
    flags = frozenset(f.strip() for f in m.group("flags").split("|") if f.strip())
    out[m.group("key")] = ParamMeta(m.group("key"), m.group("type"), flags, _strip_default(m.group("default")))
  return out


@lru_cache(maxsize=1)
def load_registry() -> dict[str, ParamMeta]:
  """key -> ParamMeta. Static header parse as the base; runtime Params refines defaults."""
  reg = dict(_parse_header())
  try:
    from openpilot.common.params import Params
    p = Params()
    runtime_keys = [k.decode("utf-8") for k in p.all_keys()]
    for key in runtime_keys:
      try:
        type_token = _TYPE_BY_ORDINAL.get(int(p.get_type(key)), "STRING")
        default = _stored_str(p.get_default_value(key), type_token)
      except Exception:
        continue
      base = reg.get(key)
      flags = base.flags if base else frozenset()
      # prefer the static literal default; only fall back to runtime when the header had none
      if base and base.default is not None:
        default = base.default
      reg[key] = ParamMeta(key, type_token, flags, default)
  except Exception:
    pass  # no compiled Params (CI/dev w/o build) — header parse stands
  return reg


def get(key: str) -> ParamMeta | None:
  return load_registry().get(key)


def exists(key: str) -> bool:
  return key in load_registry()
