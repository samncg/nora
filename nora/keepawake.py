"""Keep the machine from sleeping while Nora is running."""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger("nora.keepawake")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """Windows-only: stop idle sleep for as long as the process lives."""

    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled) and sys.platform == "win32"

    def __enter__(self) -> "KeepAwake":
        if self.enabled:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                )
                log.info("keeping the system awake while Nora runs")
            except Exception:
                log.warning("could not request system wake lock", exc_info=True)
                self.enabled = False
        return self

    def __exit__(self, *exc: object) -> None:
        if self.enabled:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
                    ES_CONTINUOUS
                )
            except Exception:
                pass
