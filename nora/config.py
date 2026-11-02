from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class PersonaConfig:
    name: str = "Nora"
    # How your persona refers to you in the system prompt.
    owner_name: str = "him"
    # Discord id of her boyfriend. Only this person gets the full
    # girlfriend treatment and the private schedule context.
    owner_id: int | None = None
    prompt_file: str = "nora/prompts/girlfriend.txt"


@dataclass
class ModelConfig:
    path: str | None = None
    gguf_dir: str = "models"
    # Vision projector. Empty = first mmproj*.gguf in gguf_dir, else no images.
    mmproj: str | None = None
    context: int = 8192
    threads: int = 8
    gpu_layers: int = 99
    flash_attn: bool = True
    temperature: float = 0.9
    top_p: float = 0.95
    top_k: int = 64
    repeat_penalty: float = 1.08
    max_tokens: int = 220
    enable_thinking: bool = False
    extra_args: list[str] = field(default_factory=list)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8081
    binary: str = "vendor/llama/llama-server.exe"
    manage: bool = True
    startup_timeout: float = 300.0
    log_file: str = "data/llama-server.log"


@dataclass
class DelayBucket:
    chance: float = 0.25
    min: float = 2.0
    max: float = 10.0


@dataclass
class ReplyDelayConfig:
    enabled: bool = True
    buckets: list[DelayBucket] = field(
        default_factory=lambda: [
            DelayBucket(chance=0.45, min=0.5, max=2.0),
            DelayBucket(chance=0.35, min=2.0, max=5.0),
            DelayBucket(chance=0.15, min=5.0, max=12.0),
            DelayBucket(chance=0.05, min=12.0, max=30.0),
        ]
    )
    max_wait: float = 30.0

    def __post_init__(self) -> None:
        # YAML hands us plain dicts, turn them into buckets.
        self.buckets = [
            bucket
            if isinstance(bucket, DelayBucket)
            else DelayBucket(**dict(bucket))
            for bucket in self.buckets
        ]


@dataclass
class ChatConfig:
    history_messages: int = 24
    history_token_budget: int = 2200
    max_chunks: int = 3
    chunk_chars: int = 190
    typing_cps: float = 11.0
    min_delay: float = 0.6
    max_delay: float = 8.0
    debounce: float = 2.0
    reply_delay: ReplyDelayConfig = field(default_factory=ReplyDelayConfig)
    # she tries to keep the thread alive instead of dead ending it
    keep_conversation: bool = True


@dataclass
class MemoryConfig:
    # one sqlite file per person, so two people can never share a context
    dir: str = "data/people"
    # fallback database, used only for non-person scopes (the terminal client)
    db_path: str = "data/nora.db"
    facts_limit: int = 40
    summaries_in_context: int = 3
    recall_hits: int = 4
    summarize_after: int = 40
    summarize_batch: int = 30


@dataclass
class FollowUpConfig:
    """She notices when you stop replying mid conversation."""

    enabled: bool = True
    # wait this long after her last text before checking on you
    after_minutes: float = 20.0
    # after this long it is no longer "where did you go", she drops it
    max_minutes: float = 240.0
    # 1 = asks where you went, 2 = also worries that something happened
    max_followups: int = 2


@dataclass
class ProactiveConfig:
    enabled: bool = True
    # Discord user id she is allowed to text out of the blue.
    user_id: int | None = None
    first_delay_minutes: float = 20.0
    min_gap_minutes: float = 150.0
    max_gap_minutes: float = 420.0
    # Only text if you have been quiet for at least this long.
    idle_minutes: float = 45.0
    quiet_hours: list[int] = field(default_factory=lambda: [0, 8])
    max_per_day: int = 4
    # Back off if she already sent this many texts with no reply.
    max_unanswered: int = 3
    chance: float = 0.75
    followup: FollowUpConfig = field(default_factory=FollowUpConfig)


@dataclass
class DiscordConfig:
    channels: list[int] = field(default_factory=list)
    respond_to_mentions: bool = True
    respond_to_dms: bool = True
    listen_all: bool = True
    # Empty = anyone may talk to her. Otherwise only these user ids.
    allow_users: list[int] = field(default_factory=list)
    token: str | None = None


@dataclass
class PresenceConfig:
    enabled: bool = True
    # custom status while she is around
    online_activity: str = "on my phone"
    online_emoji: str = "📱"
    # hours she is asleep: still answers if you text, just shows idle
    sleep_hours: list[int] = field(default_factory=lambda: [1, 8])
    idle_activity: str = "sleeping"
    idle_emoji: str = "💤"
    # optional windows where she is busy and shows dnd
    busy_hours: list[list[int]] = field(default_factory=list)
    busy_activity: str = "busy"
    busy_emoji: str = "🎧"
    refresh_minutes: float = 5.0


@dataclass
class TimetableConfig:
    enabled: bool = True
    file: str = "timetable.yaml"


@dataclass
class CalendarConfig:
    enabled: bool = True
    file: str = "calendar.yaml"


@dataclass
class TrayConfig:
    # Notification-area icon with the toggle and "text me now".
    enabled: bool = True


@dataclass
class LogConfig:
    level: str = "INFO"
    file: str = "data/nora.log"


@dataclass
class Settings:
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    presence: PresenceConfig = field(default_factory=PresenceConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    timetable: TimetableConfig = field(default_factory=TimetableConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    tray: TrayConfig = field(default_factory=TrayConfig)
    log: LogConfig = field(default_factory=LogConfig)

    # -- paths -----------------------------------------------------------
    def path(self, value: str | Path) -> Path:
        """Resolve a config path relative to the project root."""
        p = Path(value)
        return p if p.is_absolute() else (ROOT / p)

    @property
    def persona_prompt(self) -> str:
        return self.path(self.persona.prompt_file).read_text(encoding="utf-8")

    def resolve_model_path(self) -> Path:
        if self.model.path:
            return self.path(self.model.path)
        gguf_dir = self.path(self.model.gguf_dir)
        candidates = [
            p
            for p in sorted(gguf_dir.glob("*.gguf"))
            if "mmproj" not in p.name.lower() and "embed" not in p.name.lower()
        ]
        if not candidates:
            raise FileNotFoundError(
                f"No .gguf model found in {gguf_dir}.\n"
                "Run: python scripts/fetch_model.py"
            )
        return candidates[0]

    def resolve_mmproj_path(self) -> Path | None:
        """Vision projector, if one is installed."""
        if self.model.mmproj:
            path = self.path(self.model.mmproj)
            return path if path.exists() else None
        gguf_dir = self.path(self.model.gguf_dir)
        candidates = sorted(gguf_dir.glob("*mmproj*.gguf"))
        return candidates[0] if candidates else None


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively instantiate a dataclass from a plain dict."""
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        ftype = hints.get(f.name)
        if is_dataclass(ftype) and isinstance(value, dict):
            kwargs[f.name] = _build(ftype, value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def load(path: str | Path | None = None) -> Settings:
    """Load config.yaml (plus .env) into settings."""
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    data: dict[str, Any] = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:  # pragma: no cover - dotenv is optional at runtime
        pass

    settings = _build(Settings, data)
    settings.discord.token = os.environ.get("DISCORD_TOKEN") or None
    # Allow channel ids written as strings (or quoted) in YAML.
    settings.discord.channels = [int(c) for c in settings.discord.channels]
    settings.discord.allow_users = [int(c) for c in settings.discord.allow_users]
    if settings.persona.owner_id is not None:
        settings.persona.owner_id = int(settings.persona.owner_id)
    if settings.proactive.user_id is not None:
        settings.proactive.user_id = int(settings.proactive.user_id)
    quiet = list(settings.proactive.quiet_hours)[:2]
    if len(quiet) != 2:
        quiet = [0, 8]
    settings.proactive.quiet_hours = [int(quiet[0]) % 24, int(quiet[1]) % 24]

    sleep = list(settings.presence.sleep_hours)[:2]
    if len(sleep) == 2:
        settings.presence.sleep_hours = [int(sleep[0]) % 24, int(sleep[1]) % 24]
    else:
        settings.presence.sleep_hours = []
    busy: list[list[int]] = []
    for window in settings.presence.busy_hours or []:
        if isinstance(window, (list, tuple)) and len(window) >= 2:
            busy.append([int(window[0]) % 24, int(window[1]) % 24])
    settings.presence.busy_hours = busy
    return settings
