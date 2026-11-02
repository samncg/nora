"""Live settings changed from the tray, persisted to data/runtime.json.

config.yaml stays the source of truth. Anything set here is an override on top
of it, and deleting data/runtime.json brings back the config.yaml values.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import Settings

log = logging.getLogger("nora.runtime")

FREQUENCY_PRESETS: dict[str, dict[str, float]] = {
    "quiet": {"min_gap_minutes": 240.0, "max_gap_minutes": 600.0, "max_per_day": 2},
    "normal": {"min_gap_minutes": 150.0, "max_gap_minutes": 420.0, "max_per_day": 4},
    "chatty": {"min_gap_minutes": 70.0, "max_gap_minutes": 150.0, "max_per_day": 8},
}

PRESET_LABELS = {"quiet": "Quiet", "normal": "Normal", "chatty": "Chatty"}

_OVERRIDE_KEYS = ("min_gap_minutes", "max_gap_minutes", "max_per_day")


class RuntimeControls:
    """Small, file-backed override layer the tray can drive."""

    def __init__(self, settings: Settings, path: str | Path | None = None):
        self.settings = settings
        self.path = Path(path) if path else settings.path("data/runtime.json")
        self._data = self._read()
        self._apply()

    # -- persistence -----------------------------------------------------
    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except OSError:
            log.warning("could not persist runtime settings", exc_info=True)

    def _apply(self) -> None:
        cfg = self.settings.proactive
        if isinstance(self._data.get("enabled"), bool):
            cfg.enabled = self._data["enabled"]
        for key in _OVERRIDE_KEYS:
            if key not in self._data:
                continue
            try:
                value = type(getattr(cfg, key))(self._data[key])
            except (TypeError, ValueError):
                continue
            if value > 0:
                setattr(cfg, key, value)
        if cfg.max_gap_minutes < cfg.min_gap_minutes:
            cfg.max_gap_minutes = cfg.min_gap_minutes

    # -- updates ---------------------------------------------------------
    def set_proactive_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self.settings.proactive.enabled = enabled
        self._data["enabled"] = enabled
        self._write()
        log.info("proactive texting %s", "on" if enabled else "off")

    def set_frequency(self, preset: str) -> bool:
        values = FREQUENCY_PRESETS.get(preset)
        if values is None:
            return False
        for key, value in values.items():
            setattr(self.settings.proactive, key, value)
            self._data[key] = value
        self._write()
        log.info("frequency preset -> %s", preset)
        return True

    def preset_name(self) -> str | None:
        cfg = self.settings.proactive
        for name, values in FREQUENCY_PRESETS.items():
            if abs(cfg.min_gap_minutes - values["min_gap_minutes"]) < 0.01:
                return name
        return None

    def snapshot(self) -> dict[str, Any]:
        cfg = self.settings.proactive
        return {
            "enabled": cfg.enabled,
            "preset": self.preset_name(),
            "min_gap_minutes": cfg.min_gap_minutes,
            "max_gap_minutes": cfg.max_gap_minutes,
            "max_per_day": cfg.max_per_day,
        }
