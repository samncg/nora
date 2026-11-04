from __future__ import annotations

from pathlib import Path

from nora import config
from nora.llm import strip_thinking


def test_load_returns_settings():
    settings = config.load()
    assert settings.persona.name == "Nora"
    assert settings.chat.max_chunks == 3
    assert settings.memory.summarize_after > 0


def test_paths_resolve_against_project_root():
    settings = config.load()
    assert settings.path("data/nora.db").is_absolute()
    assert settings.path(settings.memory.db_path).name == "nora.db"


def test_channels_are_ints():
    settings = config.load()
    assert all(isinstance(c, int) for c in settings.discord.channels)


def test_proactive_config_parses():
    settings = config.load()
    assert settings.proactive.user_id is None or isinstance(
        settings.proactive.user_id, int
    )
    assert len(settings.proactive.quiet_hours) == 2
    assert all(isinstance(h, int) for h in settings.proactive.quiet_hours)


def test_proactive_user_id_is_parsed_when_set(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("proactive:\n  user_id: 123456789012345678\n", encoding="utf-8")
    settings = config.load(path)
    assert settings.proactive.user_id == 123456789012345678
    assert isinstance(settings.proactive.user_id, int)


def test_schedule_template_files_are_present():
    settings = config.load()
    assert settings.path(settings.timetable.file).exists()
    assert settings.path(settings.calendar.file).exists()


def test_mmproj_resolution(tmp_path):
    settings = config.load()

    missing = tmp_path / "missing.gguf"
    settings.model.mmproj = str(missing)
    assert settings.resolve_mmproj_path() is None

    present = tmp_path / "mmproj-test.gguf"
    present.write_bytes(b"x")
    settings.model.mmproj = str(present)
    assert settings.resolve_mmproj_path() == present


def test_mmproj_is_auto_detected(tmp_path):
    settings = config.load()
    settings.model.mmproj = None
    settings.model.gguf_dir = str(tmp_path)
    assert settings.resolve_mmproj_path() is None

    (tmp_path / "mmproj-model-q8_0.gguf").write_bytes(b"x")
    found = settings.resolve_mmproj_path()
    assert found is not None and found.name == "mmproj-model-q8_0.gguf"


def test_voice_config_is_parsed():
    settings = config.load()
    assert isinstance(settings.voice.enabled, bool)
    assert settings.voice.model
    assert settings.voice.max_bytes > 0


def test_tts_config_is_parsed():
    settings = config.load()
    assert isinstance(settings.tts.enabled, bool)
    assert settings.tts.voice
    assert settings.tts.model
    assert settings.tts.voices


def test_chat_timing_config_is_parsed():
    settings = config.load()
    assert settings.chat.long_gap_minutes > 0
    assert settings.chat.return_delay_max >= settings.chat.return_delay_min
    assert settings.chat.rethink_delay_max >= settings.chat.rethink_delay_min


def test_mood_config_is_parsed():
    settings = config.load()
    assert isinstance(settings.mood.enabled, bool)
    assert settings.mood.hold_minutes > 0


def test_persona_prompt_exists_and_sets_the_voice():
    settings = config.load()
    prompt = settings.persona_prompt
    assert len(prompt) > 500
    assert "lowercase" in prompt
    assert "girlfriend" in prompt
    assert "hobbies" in prompt
    # no emoji, no tsundere
    assert "never use emoji" in prompt
    assert "tsundere" not in prompt
    # she texts without full stops
    assert "never end a text with a full stop" in prompt
    # she does not invent shared plans, hobbies or recent releases
    assert "never make things up" in prompt
    assert "only the ones he has actually told you about" in prompt
    assert "never mention new releases" in prompt


def test_openrouter_config_and_env_key(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    path = tmp_path / "config.yaml"
    path.write_text(
        "provider:\n"
        "  mode: openrouter\n"
        "  openrouter:\n"
        "    model: google/gemma-4-31b-it:free\n",
        encoding="utf-8",
    )

    settings = config.load(path)

    assert settings.provider.mode == "openrouter"
    assert settings.provider.openrouter.model == "google/gemma-4-31b-it:free"
    assert settings.provider.openrouter.api_key == "sk-or-env"
    assert settings.provider.openrouter.base_url == "https://openrouter.ai/api/v1"


def test_unknown_provider_mode_falls_back_to_local(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("provider:\n  mode: somewhere-else\n", encoding="utf-8")

    assert config.load(path).provider.mode == "local"


def test_missing_model_raises_helpful_error(tmp_path):
    settings = config.load()
    settings.model.gguf_dir = str(tmp_path)
    settings.model.path = None
    try:
        settings.resolve_model_path()
    except FileNotFoundError as exc:
        assert "fetch_model.py" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected FileNotFoundError")


def test_strip_thinking_removes_channels():
    assert strip_thinking("<|channel>thought\nhm\n<channel|>hey there") == "hey there"
    assert strip_thinking("Nora: fine.") == "fine."


def test_scheduled_scripts_exist():
    root = Path(__file__).resolve().parent.parent
    for name in ("setup.ps1", "run.ps1", "install_autostart.ps1"):
        assert (root / "scripts" / name).exists()


def test_reminders_config_parses():
    settings = config.load()
    assert settings.reminders.enabled is True
    assert settings.reminders.lead_minutes == 30
    assert settings.reminders.default_hour == 9


def test_greetings_config_parses():
    settings = config.load()
    assert settings.greetings.enabled is True
    assert settings.greetings.morning_hour == 8
    assert settings.greetings.night_hour == 23
    assert 0.0 <= settings.greetings.chance <= 1.0


def test_presence_tracking_is_on_by_default():
    settings = config.load()
    assert settings.presence.track_owner is True


def test_greeting_hours_are_wrapped_and_clamped(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "greetings:\n"
        "  morning_hour: 26\n"
        "  night_hour: -1\n"
        "  chance: 4\n"
        "reminders:\n"
        "  lead_minutes: -5\n"
        "  default_hour: 25\n",
        encoding="utf-8",
    )

    settings = config.load(path)

    assert settings.greetings.morning_hour == 2
    assert settings.greetings.night_hour == 23
    assert settings.greetings.chance == 1.0
    assert settings.reminders.lead_minutes == 0
    assert settings.reminders.default_hour == 1


def test_reminder_lead_jitter_is_parsed():
    settings = config.load()
    assert settings.reminders.lead_jitter_minutes == 20
    assert settings.reminders.min_lead_minutes == 5


def test_greetings_adapt_to_his_sleep_schedule_by_default():
    settings = config.load()
    assert settings.greetings.adapt_to_sleep is True
    assert settings.greetings.min_samples == 3


def test_proactive_is_pointed_at_the_owner():
    settings = config.load()
    assert settings.proactive.user_id == settings.persona.owner_id
    assert settings.proactive.enabled is True
