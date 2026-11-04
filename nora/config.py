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
class OpenRouterConfig:
    # Empty means: set OPENROUTER_API_KEY in .env, or paste a key from the tray.
    api_key: str | None = None
    base_url: str = "https://openrouter.ai/api/v1"
    # Any model slug from openrouter.ai/models. Free models end in ":free".
    model: str = "nvidia/nemotron-3.5-lightning:free"
    referer: str = "https://github.com/samncg/nora"
    title: str = "Nora"


@dataclass
class ProviderConfig:
    # local = llama.cpp on this machine; openrouter = hosted models by API.
    mode: str = "local"
    # If a hosted request fails (rate limit, outage), answer with the local
    # model instead of leaving him with an error message.
    fallback_to_local: bool = True
    openrouter: OpenRouterConfig = field(default_factory=OpenRouterConfig)


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
    # Rhythm between her own bubbles, and an occasional typing burst that stops.
    bubble_gap_min: float = 0.4
    bubble_gap_max: float = 1.4
    typing_tease_chance: float = 0.06
    typing_tease_min: float = 0.8
    typing_tease_max: float = 1.8
    # Occasionally she stops mid-reply to think, then finishes typing. The pause
    # has to clear Discord's ~10s typing indicator to actually look like a stop.
    think_pause_chance: float = 0.08
    think_pause_min: float = 10.0
    think_pause_max: float = 18.0
    # Very rarely send a typo, then edit it a second later.
    self_correct_chance: float = 0.015
    self_correct_delay_min: float = 1.0
    self_correct_delay_max: float = 2.5
    # First reply after waking up is slower, once per day.
    waking_minutes: float = 45.0
    wake_delay_min: float = 6.0
    wake_delay_max: float = 14.0
    # When he texts over her, pause before starting the new reply.
    rethink_delay_min: float = 2.0
    rethink_delay_max: float = 4.5
    # After this much silence, her first reply takes longer; then normal speed.
    long_gap_minutes: float = 180.0
    return_delay_min: float = 5.0
    return_delay_max: float = 12.0
    reply_delay: ReplyDelayConfig = field(default_factory=ReplyDelayConfig)
    # she tries to keep the thread alive instead of dead ending it
    keep_conversation: bool = True


@dataclass
class VoiceConfig:
    # Local speech-to-text for voice notes. Nothing leaves the machine.
    enabled: bool = True
    # faster-whisper model: tiny.en / base.en / small.en / medium.en
    model: str = "base.en"
    # cpu is the safe default. cuda needs ctranslate2 to find the CUDA libs.
    device: str = "cpu"
    compute_type: str = "int8"
    # empty = detect the language automatically
    language: str = "en"
    download_root: str = "data/whisper"
    max_bytes: int = 20 * 1024 * 1024
    max_clips: int = 2


@dataclass
class TTSConfig:
    # Local Kokoro-82M text-to-speech. Used for voice replies and calls.
    # Off by default; turn it on in config.yaml.
    enabled: bool = False
    voice: str = "af_bella"
    model: str = "models/kokoro/kokoro-v1.0.onnx"
    voices: str = "models/kokoro/voices-v1.0.bin"
    language: str = "en-us"
    speed: float = 1.0
    # Vary pace and pauses slightly so consecutive bubbles do not sound flat.
    variation: float = 0.05
    sentence_pause: float = 0.3
    clause_pause: float = 0.14
    bubble_pause: float = 0.12
    # Kokoro's continuous mode keeps prosody across sentence joins.
    continuous: bool = True
    # Reply with a voice note when the user sends one.
    reply_to_voice: bool = True
    # Join a voice channel when moved into one, and speak replies there.
    voice_channel: bool = True
    greeting: str = "hey im here"


@dataclass
class ReactionConfig:
    # React to some of his messages, and remember when he reacts to hers.
    enabled: bool = True
    chance: float = 0.18
    cooldown_minutes: float = 20.0
    # Reactions land a few seconds later, after she has read the message,
    # instead of firing the instant it arrives.
    delay_min: float = 4.0
    delay_max: float = 11.0
    remember_user_reactions: bool = True


@dataclass
class MoodConfig:
    # A slow-moving mood per person. It tints her tone and typing pace.
    enabled: bool = True
    hold_minutes: int = 180


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
    # See your status and what you are playing. Needs the Presence and Server
    # Members intents enabled in the Discord developer portal.
    track_owner: bool = True


@dataclass
class RemindersConfig:
    """She remembers things he mentions and texts him before they start."""

    enabled: bool = True
    # how long before the event the reminder goes out, on average
    lead_minutes: int = 30
    # the actual lead is picked at random inside lead_minutes +/- this, so it
    # does not look like a scheduled notification every single time
    lead_jitter_minutes: int = 20
    # never leave it later than this before the event
    min_lead_minutes: int = 5
    # when he only says the day ("exam tomorrow"), assume this hour
    default_hour: int = 9
    # how many days ahead of an event she may mention it in the prompt
    prompt_days: float = 2.0


@dataclass
class GreetingsConfig:
    """Good morning and goodnight, once a day each."""

    enabled: bool = True
    morning_hour: int = 8
    morning_window_hours: float = 3.0
    night_hour: int = 23
    night_window_hours: float = 2.0
    chance: float = 1.0
    # skip it if you two were already texting this recently
    skip_if_chatted_minutes: float = 30.0
    # move the two windows to follow when he actually says good morning and
    # goodnight, instead of the fixed hours above
    adapt_to_sleep: bool = True
    # how many of his greetings to learn from before the window moves
    min_samples: int = 3


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
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    reactions: ReactionConfig = field(default_factory=ReactionConfig)
    mood: MoodConfig = field(default_factory=MoodConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    presence: PresenceConfig = field(default_factory=PresenceConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    reminders: RemindersConfig = field(default_factory=RemindersConfig)
    greetings: GreetingsConfig = field(default_factory=GreetingsConfig)
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

    mode = str(settings.provider.mode or "local").strip().lower()
    settings.provider.mode = mode if mode in {"local", "openrouter"} else "local"
    openrouter = settings.provider.openrouter
    openrouter.model = (openrouter.model or "").strip()
    openrouter.base_url = (
        openrouter.base_url or "https://openrouter.ai/api/v1"
    ).strip().rstrip("/")
    if openrouter.api_key:
        openrouter.api_key = openrouter.api_key.strip()
    env_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if env_key:
        openrouter.api_key = env_key
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

    reminders = settings.reminders
    reminders.default_hour = int(reminders.default_hour) % 24
    reminders.lead_minutes = max(0, int(reminders.lead_minutes))
    reminders.lead_jitter_minutes = max(0, int(reminders.lead_jitter_minutes))
    reminders.min_lead_minutes = max(0, int(reminders.min_lead_minutes))
    reminders.prompt_days = max(0.0, float(reminders.prompt_days))

    greetings = settings.greetings
    greetings.morning_hour = int(greetings.morning_hour) % 24
    greetings.night_hour = int(greetings.night_hour) % 24
    greetings.morning_window_hours = max(0.0, float(greetings.morning_window_hours))
    greetings.night_window_hours = max(0.0, float(greetings.night_window_hours))
    greetings.chance = max(0.0, min(1.0, float(greetings.chance)))
    greetings.skip_if_chatted_minutes = max(
        0.0, float(greetings.skip_if_chatted_minutes)
    )
    greetings.min_samples = max(1, int(greetings.min_samples))
    return settings
