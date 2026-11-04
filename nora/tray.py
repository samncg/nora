"""Windows notification-area icon: toggle, quick settings, "text me now".

Runs inside the bot process, so the menu always reflects real state and the
actions go straight to the running event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - optional at runtime
    pystray = None

from . import dialogs
from .llm import openrouter_free_models, probe_openrouter
from .runtime import PRESET_LABELS

log = logging.getLogger("nora.tray")

COLORS = {
    "online": "#2ea043",
    "paused": "#3d6fa8",
    "offline": "#6e7681",
}

_FONT_CANDIDATES = ("arialbd.ttf", "arial.ttf", "segoeuib.ttf", "segoeui.ttf")


def _font(size: int) -> Any:
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_icon(color: str) -> Any:
    """A simple round badge with an N, drawn at 64px and scaled by Windows."""
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, 62, 62), fill=color)
    font = _font(38)
    box = draw.textbbox((0, 0), "N", font=font)
    width, height = box[2] - box[0], box[3] - box[1]
    draw.text(
        ((64 - width) / 2 - box[0], (64 - height) / 2 - box[1]),
        "N",
        font=font,
        fill="white",
    )
    return image


class NoraTray:
    """Owns the icon thread and translates menu clicks into bot actions."""

    def __init__(self, bot: Any, loop: asyncio.AbstractEventLoop):
        self.bot = bot
        self.loop = loop
        self.icon: Any = None
        self._thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict[str, Any] = {}
        self._images: dict[str, Any] = {}
        self._items: dict[str, Any] = {}
        self._preset_items: dict[str, Any] = {}

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if pystray is None:
            raise RuntimeError("pystray is not installed")
        self.icon = pystray.Icon(
            "nora", self._image("offline"), "Nora", menu=self._menu()
        )
        self._thread = threading.Thread(
            target=self._run, name="nora-tray", daemon=True
        )
        self._thread.start()
        self._poll_thread = threading.Thread(
            target=self._poll, name="nora-tray-poll", daemon=True
        )
        self._poll_thread.start()

    def _run(self) -> None:
        try:
            self.icon.run()
        except Exception:
            log.exception("tray icon stopped unexpectedly")

    def stop(self) -> None:
        self._stop.set()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass

    # -- menu ------------------------------------------------------------
    def _menu(self) -> Any:
        # pystray evaluates callables each time the menu is refreshed, which is
        # how the disabled header and the enabled state stay in sync.
        self._items["status"] = pystray.MenuItem(
            self._headline, None, enabled=False
        )
        self._items["text_now"] = pystray.MenuItem(
            "Text me now",
            self._on_text_now,
            default=True,
            enabled=lambda item: bool(self._status.get("ready")),
        )
        self._items["proactive"] = pystray.MenuItem(
            "Proactive texts",
            self._on_toggle_proactive,
            checked=lambda item: bool(self.bot.settings.proactive.enabled),
        )

        presets = pystray.Menu(
            *[
                self._preset_item(name, label)
                for name, label in PRESET_LABELS.items()
            ]
        )

        provider = pystray.Menu(
            pystray.MenuItem(
                "Local llama.cpp",
                self._on_use_local,
                checked=self._local_checked,
                radio=True,
            ),
            pystray.MenuItem(
                "OpenRouter",
                self._on_use_openrouter,
                checked=self._openrouter_checked,
                radio=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._provider_label, None, enabled=False),
            pystray.MenuItem("Choose OpenRouter model...", self._on_choose_model),
            pystray.MenuItem("Set OpenRouter API key...", self._on_set_key),
            pystray.MenuItem("Test OpenRouter", self._on_test_openrouter),
        )

        return pystray.Menu(
            self._items["status"],
            pystray.Menu.SEPARATOR,
            self._items["text_now"],
            self._items["proactive"],
            pystray.MenuItem("How often she texts", presets),
            pystray.MenuItem("Model provider", provider),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open config.yaml", self._on_open_config),
            pystray.MenuItem("Open log", self._on_open_log),
            pystray.MenuItem("Open data folder", self._on_open_data),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart bot", self._on_restart),
            pystray.MenuItem("Quit tray", self._on_quit),
        )

    def _preset_item(self, name: str, label: str) -> Any:
        item = pystray.MenuItem(
            label,
            self._preset_action(name),
            checked=self._preset_checked(name),
            radio=True,
        )
        self._preset_items[name] = item
        return item

    def _preset_action(self, name: str) -> Any:
        def action(icon: Any, item: Any) -> None:
            self.bot.controls.set_frequency(name)
            self._refresh_now()

        return action

    def _preset_checked(self, name: str) -> Any:
        return lambda item: self.bot.controls.preset_name() == name

    # -- status polling --------------------------------------------------
    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_now()
            except Exception:
                log.debug("tray refresh failed", exc_info=True)
            self._stop.wait(5)

    def _refresh_now(self) -> None:
        status: dict[str, Any] = {}
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.bot.tray_status(), self.loop
            )
            status = future.result(timeout=10) or {}
        except Exception:
            status = {}
        self._apply_status(status)

    def _apply_status(self, status: dict[str, Any]) -> None:
        self._status = status or {}
        if self.icon is None:
            return
        self.icon.icon = self._image(self._color())
        self.icon.title = self._tooltip()
        self.icon.update_menu()

    def _color(self) -> str:
        if not self._status.get("ready"):
            return "offline"
        if not self._status.get("proactive_enabled"):
            return "paused"
        return "online"

    def _headline(self, item: Any = None) -> str:
        status = self._status
        if not status.get("ready"):
            return "Nora: offline"
        if status.get("provider") == "openrouter":
            if not status.get("model_ready"):
                return "Nora: openrouter needs a key/model"
            return f"Nora: online, {status.get('provider_model') or 'openrouter'}"
        if not status.get("model_ready"):
            return "Nora: online, model loading"
        if not status.get("proactive_enabled"):
            return "Nora: online, not texting first"
        return f"Nora: online, {status.get('sent_today', 0)} sent today"

    def _tooltip(self) -> str:
        status = self._status
        if not status.get("ready"):
            return "Nora - bot offline"
        parts = ["Nora - online"]
        if status.get("provider") == "openrouter":
            parts.append(f"openrouter {status.get('provider_model') or ''}".strip())
        if status.get("proactive_enabled"):
            sent = status.get("sent_today", 0)
            cap = status.get("max_per_day", 0)
            parts.append(f"texts you {sent}/{cap} today")
            if status.get("quiet"):
                parts.append("quiet hours")
            elif status.get("next_in_minutes") is not None:
                parts.append(f"next in ~{status['next_in_minutes']}m")
        else:
            parts.append("proactive off")
        return ", ".join(parts)

    def _image(self, color_key: str) -> Any:
        if color_key not in self._images:
            self._images[color_key] = make_icon(COLORS[color_key])
        return self._images[color_key]

    # -- actions ---------------------------------------------------------
    def _submit(self, coro: Any) -> None:
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        except Exception:
            log.warning("could not reach the bot loop", exc_info=True)
            coro.close()

    def _on_text_now(self, icon: Any, item: Any) -> None:
        log.info("text-me-now clicked in the tray")
        self._submit(self.bot.text_me_now())

    def _on_toggle_proactive(self, icon: Any, item: Any) -> None:
        enabled = not bool(self.bot.settings.proactive.enabled)
        self.bot.set_proactive_enabled(enabled)
        self._refresh_now()

    # -- model provider --------------------------------------------------
    def _local_checked(self, item: Any = None) -> bool:
        return self.bot.settings.provider.mode != "openrouter"

    def _openrouter_checked(self, item: Any = None) -> bool:
        return self.bot.settings.provider.mode == "openrouter"

    def _provider_label(self, item: Any = None) -> str:
        provider = self.bot.settings.provider
        if provider.mode != "openrouter":
            return "Local llama.cpp"
        model = provider.openrouter.model or "no model"
        key = "key set" if (provider.openrouter.api_key or "").strip() else "no key"
        return f"OpenRouter: {model} ({key})"

    def _run_dialog(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run Tk off the pystray thread and wait for the result."""
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["value"] = fn(*args, **kwargs)
            except Exception:
                log.exception("dialog failed")

        thread = threading.Thread(target=run, name="nora-dialog", daemon=True)
        thread.start()
        thread.join()
        return box.get("value")

    def _notify(self, message: str) -> None:
        try:
            if self.icon is not None:
                self.icon.notify(message, "Nora")
        except Exception:
            log.debug("could not show a tray notification", exc_info=True)

    def _ask_key(self) -> str | None:
        provider = self.bot.settings.provider.openrouter
        value = self._run_dialog(
            dialogs.ask_text,
            "OpenRouter API key",
            "Paste your OpenRouter key (sk-or-...). It is saved locally in data/runtime.json.",
            provider.api_key or "",
            True,
        )
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _on_use_local(self, icon: Any, item: Any) -> None:
        self.bot.controls.set_provider("local")
        self._submit(self.bot.warm_model())
        self._refresh_now()

    def _on_use_openrouter(self, icon: Any, item: Any) -> None:
        provider = self.bot.settings.provider.openrouter
        if not (provider.api_key or "").strip():
            key = self._ask_key()
            if not key:
                self._refresh_now()
                return
            self.bot.controls.set_openrouter_key(key)
        if not (provider.model or "").strip():
            self.bot.controls.set_openrouter_model(
                "nvidia/nemotron-3.5-lightning:free"
            )
        self.bot.controls.set_provider("openrouter")
        self._submit(self.bot.stop_local_model())
        self._refresh_now()

    def _on_set_key(self, icon: Any, item: Any) -> None:
        key = self._ask_key()
        if key:
            self.bot.controls.set_openrouter_key(key)
            self._notify("OpenRouter key saved.")
        self._refresh_now()

    def _on_choose_model(self, icon: Any, item: Any) -> None:
        current = self.bot.settings.provider.openrouter.model or ""
        options = openrouter_free_models()
        if current and current not in options:
            options = [current] + options
        value = self._run_dialog(dialogs.choose_model, options, current)
        if isinstance(value, str) and value.strip():
            chosen = value.strip()
            self.bot.controls.set_openrouter_model(chosen)
            self._notify(f"Model set to {chosen}")
        self._refresh_now()

    def _on_test_openrouter(self, icon: Any, item: Any) -> None:
        ok, message = probe_openrouter(self.bot.settings)
        prefix = "OpenRouter works: " if ok else "OpenRouter failed: "
        self._notify(prefix + message)
        self._refresh_now()

    def _on_restart(self, icon: Any, item: Any) -> None:
        log.info("restart requested from the tray")
        self._submit(self.bot.close())

    def _on_quit(self, icon: Any, item: Any) -> None:
        log.info("tray closed from the menu")
        self.stop()

    def _on_open_config(self, icon: Any, item: Any) -> None:
        self._open(self.bot.settings.path("config.yaml"))

    def _on_open_log(self, icon: Any, item: Any) -> None:
        self._open(self.bot.settings.path(self.bot.settings.log.file))

    def _on_open_data(self, icon: Any, item: Any) -> None:
        self._open(self.bot.settings.path("data"))

    @staticmethod
    def _open(path: Any) -> None:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:
            log.warning("could not open %s", path, exc_info=True)
