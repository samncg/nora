from __future__ import annotations

import pytest

pytest.importorskip("pystray")

from nora import config
from nora.tray import COLORS, NoraTray, make_icon


class FakeControls:
    def __init__(self) -> None:
        self.preset = "normal"
        self.frequency_calls: list[str] = []

    def set_frequency(self, name: str) -> bool:
        self.frequency_calls.append(name)
        self.preset = name
        return True

    def preset_name(self) -> str:
        return self.preset


class FakeBot:
    def __init__(self, tmp_path) -> None:
        self.settings = config.load()
        self.settings.memory.db_path = str(tmp_path / "n.db")
        self.controls = FakeControls()
        self.toggles: list[bool] = []

    def set_proactive_enabled(self, enabled: bool) -> None:
        self.settings.proactive.enabled = enabled
        self.toggles.append(enabled)

    async def text_me_now(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def tray_status(self) -> dict:
        return {}


class FakeIcon:
    def __init__(self) -> None:
        self.icon = None
        self.title = None
        self.updates = 0

    def update_menu(self) -> None:
        self.updates += 1


@pytest.fixture()
def tray(tmp_path):
    bot = FakeBot(tmp_path)
    instance = NoraTray(bot, loop=None)
    instance._menu()  # real pystray menu items, no window needed
    instance.icon = FakeIcon()
    instance._refresh_now = lambda: None
    return instance


def test_make_icon_size_and_color():
    image = make_icon(COLORS["online"])
    assert image.size == (64, 64)
    assert image.mode == "RGBA"


def test_status_text_when_online(tray):
    tray._apply_status(
        {
            "ready": True,
            "model_ready": True,
            "proactive_enabled": True,
            "sent_today": 2,
            "max_per_day": 4,
            "next_in_minutes": 35,
        }
    )
    assert tray._color() == "online"
    assert tray._headline() == "Nora: online, 2 sent today"
    assert "2/4" in tray._tooltip()
    assert "35m" in tray._tooltip()
    assert tray._items["text_now"].enabled is True
    assert tray.icon.updates >= 1


def test_status_text_when_paused(tray):
    tray._apply_status(
        {"ready": True, "model_ready": True, "proactive_enabled": False}
    )
    assert tray._color() == "paused"
    assert "not texting first" in tray._headline()
    assert "proactive off" in tray._tooltip()


def test_status_text_when_offline(tray):
    tray._apply_status({"ready": False})
    assert tray._color() == "offline"
    assert tray._headline() == "Nora: offline"
    assert tray._items["text_now"].enabled is False


def test_status_text_while_model_loads(tray):
    tray._apply_status({"ready": True, "model_ready": False, "proactive_enabled": True})
    assert "model loading" in tray._headline()


def test_quiet_hours_show_in_tooltip(tray):
    tray._apply_status(
        {
            "ready": True,
            "model_ready": True,
            "proactive_enabled": True,
            "sent_today": 0,
            "max_per_day": 4,
            "quiet": True,
        }
    )
    assert "quiet hours" in tray._tooltip()


def test_openrouter_provider_label_and_headline(tray):
    provider = tray.bot.settings.provider
    provider.mode = "openrouter"
    provider.openrouter.model = "google/gemma-4-31b-it:free"
    provider.openrouter.api_key = "sk-or-test"

    assert "gemma-4-31b-it" in tray._provider_label()
    assert "key set" in tray._provider_label()
    assert tray._openrouter_checked() is True
    assert tray._local_checked() is False

    tray._apply_status(
        {
            "ready": True,
            "model_ready": True,
            "provider": "openrouter",
            "provider_model": "google/gemma-4-31b-it:free",
            "proactive_enabled": True,
        }
    )
    assert "gemma-4-31b-it" in tray._headline()
    assert "openrouter" in tray._tooltip().lower()


def test_openrouter_without_key_is_not_ready(tray):
    provider = tray.bot.settings.provider
    provider.mode = "openrouter"
    provider.openrouter.model = "google/gemma-4-31b-it:free"
    provider.openrouter.api_key = None

    tray._apply_status(
        {
            "ready": True,
            "model_ready": False,
            "provider": "openrouter",
            "provider_model": provider.openrouter.model,
            "proactive_enabled": True,
        }
    )
    assert "needs a key" in tray._headline()


def test_toggle_flips_proactive(tray):
    assert tray.bot.settings.proactive.enabled is True

    tray._on_toggle_proactive(None, None)
    assert tray.bot.settings.proactive.enabled is False
    assert tray.bot.toggles == [False]

    tray._on_toggle_proactive(None, None)
    assert tray.bot.settings.proactive.enabled is True
    assert tray.bot.toggles == [False, True]


def test_text_me_now_submits_job(tray):
    submitted: list[str] = []

    def fake_submit(coro):
        submitted.append(coro.cr_code.co_name)
        coro.close()

    tray._submit = fake_submit
    tray._on_text_now(None, None)
    assert submitted == ["text_me_now"]


def test_restart_submits_shutdown(tray):
    submitted: list[str] = []

    def fake_submit(coro):
        submitted.append(coro.cr_code.co_name)
        coro.close()

    tray._submit = fake_submit
    tray._on_restart(None, None)
    assert submitted == ["close"]


def test_preset_action_sets_frequency(tray):
    tray._preset_action("chatty")(None, None)
    assert tray.bot.controls.frequency_calls == ["chatty"]
    assert tray.bot.controls.preset_name() == "chatty"


def test_preset_radio_tracks_current_preset(tray):
    checked = {name: tray._preset_checked(name) for name in ("quiet", "normal", "chatty")}
    assert checked["normal"](None) is True
    assert checked["chatty"](None) is False

    tray.bot.controls.set_frequency("chatty")
    assert checked["chatty"](None) is True
