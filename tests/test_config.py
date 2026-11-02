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


def test_proactive_config_is_unset_by_default():
    settings = config.load()
    assert settings.proactive.user_id is None
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
