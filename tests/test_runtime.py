from __future__ import annotations

import json

from nora import config
from nora.runtime import RuntimeControls


def make_settings(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "nora.db")
    return settings


def test_defaults_come_from_config(tmp_path):
    settings = make_settings(tmp_path)
    controls = RuntimeControls(settings, path=tmp_path / "runtime.json")

    assert settings.proactive.enabled is True
    assert controls.preset_name() == "normal"
    # nothing written until the user actually changes something
    assert not (tmp_path / "runtime.json").exists()


def test_toggle_persists_and_reloads(tmp_path):
    path = tmp_path / "runtime.json"
    settings = make_settings(tmp_path)

    RuntimeControls(settings, path=path).set_proactive_enabled(False)
    assert settings.proactive.enabled is False
    assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is False

    reloaded = make_settings(tmp_path)
    RuntimeControls(reloaded, path=path)
    assert reloaded.proactive.enabled is False


def test_frequency_preset_applies_and_persists(tmp_path):
    path = tmp_path / "runtime.json"
    settings = make_settings(tmp_path)
    controls = RuntimeControls(settings, path=path)

    assert controls.set_frequency("chatty") is True
    assert settings.proactive.min_gap_minutes == 70
    assert settings.proactive.max_gap_minutes == 150
    assert settings.proactive.max_per_day == 8
    assert controls.preset_name() == "chatty"

    reloaded = make_settings(tmp_path)
    RuntimeControls(reloaded, path=path)
    assert reloaded.proactive.min_gap_minutes == 70
    assert reloaded.proactive.max_per_day == 8


def test_unknown_preset_is_rejected(tmp_path):
    settings = make_settings(tmp_path)
    controls = RuntimeControls(settings, path=tmp_path / "runtime.json")
    before = settings.proactive.min_gap_minutes

    assert controls.set_frequency("never") is False
    assert settings.proactive.min_gap_minutes == before


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text("{not json", encoding="utf-8")
    settings = make_settings(tmp_path)

    controls = RuntimeControls(settings, path=path)
    assert settings.proactive.enabled is True
    assert controls.preset_name() == "normal"


def test_invalid_values_are_ignored(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps({"min_gap_minutes": -5, "max_gap_minutes": "x", "max_per_day": 0}),
        encoding="utf-8",
    )
    settings = make_settings(tmp_path)

    RuntimeControls(settings, path=path)
    assert settings.proactive.min_gap_minutes == 150
    assert settings.proactive.max_gap_minutes == 420
    assert settings.proactive.max_per_day == 4


def test_snapshot_reports_current_values(tmp_path):
    settings = make_settings(tmp_path)
    controls = RuntimeControls(settings, path=tmp_path / "runtime.json")

    snapshot = controls.snapshot()
    assert snapshot["enabled"] is True
    assert snapshot["preset"] == "normal"
    assert snapshot["max_per_day"] == 4
