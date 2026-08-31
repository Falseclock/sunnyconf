"""
sunnyconf.device — device identity, so a client knows exactly which comma it is connecting to.

Everything is derived from the running hardware + Params — nothing about a specific device is hardcoded.
Used by /status (full detail) and by the mDNS instance name + TXT (so the device is identifiable during
discovery, before you connect).
"""
from __future__ import annotations

from openpilot.common.params import Params

# comma AGNOS device type -> friendly model name (fallback: the raw type). The ONLY device-name mapping.
_DEVICE_NAMES = {"tici": "comma3", "tizi": "comma3x", "mici": "comma4"}


def device_type() -> str:
  try:
    from openpilot.system.hardware import HARDWARE
    return (HARDWARE.get_device_type() or "").strip()
  except Exception:
    return ""


def device_model() -> str:
  dt = device_type()
  return _DEVICE_NAMES.get(dt, dt or "comma")


def _g(params: Params, key: str) -> str:
  try:
    return params.get(key) or ""
  except Exception:
    return ""


def device_info(params: Params) -> dict:
  """Identity fields for /status."""
  return {
    "model": device_model(),                       # comma3 / comma3x / comma4 (auto-detected)
    "device_type": device_type(),                  # tici / tizi / mici
    "dongle_id": _g(params, "DongleId"),           # comma Dongle ID
    "sunnylink_id": _g(params, "SunnylinkDongleId"),  # sunnylink Device ID
    "serial": _g(params, "HardwareSerial"),
    "version": _g(params, "Version"),              # software version string (e.g. 2026.001.000)
  }


def instance_name(params: Params) -> str:
  """mDNS instance name: <model>-<short id>, unique + identifiable (never hardcoded)."""
  info = device_info(params)
  sid = (info["dongle_id"] or info["serial"] or "")[:8]
  return f"{info['model']}-{sid}" if sid else info["model"]


def mdns_txt(params: Params) -> dict:
  """Compact identity for the mDNS TXT record — device type + ids only (visible during discovery, before
  connecting). schema_version / commit are available from /status after connecting, not needed here."""
  info = device_info(params)
  return {
    "model": info["model"],
    "dongle_id": info["dongle_id"],
    "sunnylink_id": info["sunnylink_id"],
  }
