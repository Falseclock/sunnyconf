"""Schema generation (SDUI-driven), value coercion, offroad gating, and change-detection tests."""
import pytest

from openpilot.sunnyconf.daemon import schema_gen, values, sdui

CONTRACT_FIELDS = {"key", "title", "description", "type", "options", "min", "max", "step", "unit",
                   "default", "offroad_only", "requires_restart", "auto_detected",
                   "section", "readonly", "enablement", "visible_when"}
TYPES = {"bool", "int", "float", "enum", "string"}


def test_schema_contract_shape():
  s = schema_gen.build_schema(refresh=True)
  assert s["schema_version"] == 1
  assert {"commit", "branch", "generated_at"} <= set(s["sunnypilot"])
  assert isinstance(s["groups"], list) and s["groups"], "expected non-empty groups"
  for g in s["groups"]:
    assert {"id", "title", "params"} <= set(g)
    for p in g["params"]:
      assert CONTRACT_FIELDS <= set(p), f"missing fields on {p.get('key')}"
      assert p["type"] in TYPES
      assert isinstance(p["options"], list)


def test_sdui_is_primary_and_covers_manual_toggles():
  # SDUI reaches params the raylib-layout AST cannot (manually-bound toggles)
  assert len(sdui.build_sdui_index()) > 60
  idx = schema_gen.index_by_key(include_all=False)
  assert len(idx) >= 75, "SDUI pivot should surface ~85 user-facing params"
  # AdbEnabled / JoystickDebugMode bind their param manually in the layouts (no param= kwarg) -> only SDUI sees them
  assert "AdbEnabled" in idx and idx["AdbEnabled"].entry["type"] == "bool"
  assert "JoystickDebugMode" in idx


def test_widget_type_mapping():
  idx = schema_gen.index_by_key(include_all=False)
  assert idx["Mads"].entry["type"] == "bool"
  # multiple_button -> enum with option value/label pairs
  mode = idx["MadsSteeringMode"].entry
  assert mode["type"] == "enum" and len(mode["options"]) >= 2
  # option widget on a FLOAT param -> float with min/max/unit straight from SDUI
  torque = idx["TorqueParamsOverrideLatAccelFactor"].entry
  assert torque["type"] == "float" and torque["min"] == 0.1 and torque["max"] == 5.0 and torque["unit"] == "m/s²"
  # info widget -> read-only
  assert idx["LanguageSetting"].entry["readonly"] is True


def test_offroad_only_declarative():
  idx = schema_gen.index_by_key(include_all=False)
  # from SDUI enablement:[{type:offroad_only}] — no AST heuristic
  for k in ("Mads", "IntelligentCruiseButtonManagement", "AdbEnabled", "MadsSteeringMode"):
    assert idx[k].offroad_only is True, f"{k} should be offroad_only"
  off = [k for k, ie in idx.items() if ie.offroad_only]
  assert len(off) >= 17


def test_groups_from_sdui_panels():
  s = schema_gen.build_schema()
  by_id = {g["id"]: g for g in s["groups"]}
  assert "steering" in by_id and by_id["steering"]["title"] == "Steering"
  assert "vehicle" in by_id  # from vehicle_settings
  # a vehicle param carries its brand section
  idx = schema_gen.index_by_key(include_all=False)
  assert "Hyundai" in idx["HyundaiLongitudinalTuning"].entry["section"]


def test_value_validation():
  idx = schema_gen.index_by_key(include_all=False)
  b = idx["SmartCruiseControlVision"]
  assert values.parse_incoming(b.entry, b.type_token, "true") is True
  assert values.parse_incoming(b.entry, b.type_token, "0") is False
  # float option: in range ok, out of range rejected
  t = idx["TorqueParamsOverrideLatAccelFactor"]
  assert values.parse_incoming(t.entry, t.type_token, "1.5") == 1.5
  with pytest.raises(ValueError):
    values.parse_incoming(t.entry, t.type_token, "9.9")   # > max 5.0
  # enum: valid option accepted, bogus rejected
  m = idx["MadsSteeringMode"]
  ok = m.entry["options"][0][0]
  assert str(values.parse_incoming(m.entry, m.type_token, ok)) == ok
  with pytest.raises(ValueError):
    values.parse_incoming(m.entry, m.type_token, "999")


def test_overrides_surface_non_sdui_keys():
  # the handful of keys not in settings_ui.json are surfaced via schema_overrides.json into their panel
  idx = schema_gen.index_by_key(include_all=False)
  assert idx["SunnylinkEnabled"].group_id == "sunnylink"
  assert idx["BlinkerMinLateralControlSpeed"].group_id == "steering"


def test_include_all_superset():
  menu = set(schema_gen.index_by_key(include_all=False))
  every = set(schema_gen.index_by_key(include_all=True))
  registry_keys = set(schema_gen.reg.load_registry())
  assert menu <= every
  assert len(every) >= len(registry_keys) - 5   # ~all registered keys reachable
