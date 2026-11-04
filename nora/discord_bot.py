"""Discord transport: debounced replies, typing delays, memory commands."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import random
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from . import activity as activity_mod
from . import greetings as greetings_mod
from . import pacing
from . import presence
from . import proactive as proactive_mod
from . import reminders as reminders_mod
from .asr import Transcriber
from .brain import PHOTO_MARKER, Nora
from .config import ProactiveConfig, Settings
from .humanize import typing_seconds
from .persona import MORNING_OCCASION, NIGHT_OCCASION, REMINDER_OCCASION
from .runtime import RuntimeControls
from .tts import Speech, Speaker, pcm_48k_stereo
from .vision import MAX_BYTES as MAX_IMAGE_BYTES
from .vision import MAX_IMAGES

log = logging.getLogger("nora.discord")

_MENTION_RE = re.compile(r"<@[!&]?\d+>")
_SPACE_RE = re.compile(r"[ \t]{2,}")

Pending = list[tuple[str, int | None, str | None, list[bytes], bool]]

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO_SUFFIXES = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".webm", ".flac", ".aac"}

# Each emoji only fires for a matching tone. Order matters: the first cue that
# matches decides the reaction, and no cue means she does not react at all.
REACTION_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "❤️",
        (
            "love you",
            "miss you",
            "ily",
            "thinking of you",
            "thinking about you",
            "cuddle",
            "kiss",
            "sweet",
            "you're the best",
            "youre the best",
        ),
    ),
    (
        "🔥",
        (
            "got the job",
            "passed",
            "won",
            "let's go",
            "lets go",
            "great news",
            "good news",
            "proud of",
            "did it",
        ),
    ),
    (
        "😭",
        (
            "sad",
            "crying",
            "cry",
            "tired",
            "exhausted",
            "stressed",
            "bad day",
            "rough day",
            "lonely",
            "awful",
            "overwhelmed",
        ),
    ),
    (
        "💀",
        (
            "lol",
            "lmao",
            "dead",
            "bruh",
            "no way",
            "crazy",
            "insane",
            "wtf",
        ),
    ),
)

# Saying bye always gets a heart: no dice roll, no cooldown.
FAREWELL_CUES = (
    "goodbye",
    "good bye",
    "bye",
    "ttyl",
    "talk later",
    "talk to you later",
    "see you",
    "see ya",
    "goodnight",
    "good night",
    "night night",
    "cya",
    "gotta go",
    "got to go",
    "gtg",
    "heading out",
    "signing off",
)


def _cue_in(phrase: str, text: str) -> bool:
    """Whole-word match for single words, plain substring for phrases."""
    if " " in phrase:
        return phrase in text
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


class NoraBot(commands.Bot):
    # how many times she may rewrite herself within one burst
    MAX_ADAPTATIONS = 2

    def __init__(self, settings: Settings, nora: Nora, *, privileged: bool = True):
        intents = discord.Intents.default()
        # Privileged intent: enable it in the Developer Portal too.
        intents.message_content = True
        intents.dm_messages = True
        intents.reactions = True
        # Reading his status needs presences + members, which are privileged
        # as well. If the portal has them off, discord closes with 4014, so
        # the caller may retry us without them.
        self._track_owner = bool(privileged and settings.presence.track_owner)
        intents.members = self._track_owner
        intents.presences = self._track_owner
        super().__init__(command_prefix="!", intents=intents, help_command=None)

        self.settings = settings
        self.nora = nora
        self.transcriber = Transcriber(settings)
        self.speaker = Speaker(settings)
        self.started_at = time.time()
        self.controls = RuntimeControls(settings)
        self._pending: dict[int, Pending] = {}
        self._pending_since: dict[int, float] = {}
        self._timer_tasks: dict[int, asyncio.Task[None]] = {}
        self._versions: dict[int, int] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._voice_locks: dict[int, asyncio.Lock] = {}
        self._proactive_task: asyncio.Task[None] | None = None
        self._reminder_task: asyncio.Task[None] | None = None
        self._greeting_task: asyncio.Task[None] | None = None
        self._presence_task: asyncio.Task[None] | None = None
        self._presence_key: tuple[str, str] | None = None
        self._owner_activity: str | None = None
        # starting hours guessed from his message history, before she has
        # heard him say good morning or goodnight enough times
        self._greeting_seed: dict[str, float] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._direct_channel: Any = None
        self._proactive_uid: int | None = None
        self._last_proactive: datetime | None = None
        self._next_proactive_at: datetime | None = None
        self._sent_today = 0
        self._sent_day: date | None = None
        self._followups: dict[str, int] = {}
        self._followup_at: dict[str, datetime] = {}
        self._tray: Any = None
        self._voice_client: Any = None
        self._voice_channel_id: int | None = None
        self._voice_speak_lock: asyncio.Lock | None = None
        self._last_reaction: dict[str, datetime] = {}
        self._reaction_tasks: set[asyncio.Task[None]] = set()
        self._wake_delay_used: dict[str, date] = {}
        # discord.py bundles libopus on Windows but does not load it until asked.
        if not discord.opus.is_loaded():
            try:
                discord.opus._load_default()
            except Exception:
                log.warning("could not load libopus", exc_info=True)

    # -- lifecycle -------------------------------------------------------
    async def setup_hook(self) -> None:
        self.tree.add_command(
            app_commands.Command(
                name="recall",
                description="search everything you two ever talked about",
                callback=self._cmd_recall,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="memory",
                description="show what she remembers about you",
                callback=self._cmd_memory,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="forget",
                description="drop one remembered fact by id",
                callback=self._cmd_forget,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="status",
                description="is she awake, and what does she remember",
                callback=self._cmd_status,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="reminders",
                description="what she is going to remind you about",
                callback=self._cmd_reminders,
            )
        )
        if self.settings.tts.enabled and self.settings.tts.voice_channel:
            self.tree.add_command(
                app_commands.Command(
                    name="call",
                    description="bring her into your voice channel",
                    callback=self._cmd_call,
                )
            )
            self.tree.add_command(
                app_commands.Command(
                    name="leave",
                    description="make her leave the voice channel",
                    callback=self._cmd_leave,
                )
            )
        try:
            synced = await self.tree.sync()
            log.info("synced %d slash commands", len(synced))
        except Exception:
            log.exception("slash command sync failed")

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        if self.user:
            await self._apply_presence()
            if self.settings.presence.enabled:
                self._presence_task = asyncio.create_task(self._presence_loop())
            self._scan_owner_presence()
            # Icon first, so it is there while the model is still loading.
            self._start_tray()
            try:
                await self.warm_model()
            finally:
                self._start_proactive()
                self._start_scheduler()
                self._stop_task = asyncio.create_task(self._watch_stop_file())

    async def warm_model(self) -> None:
        """Start llama-server for local answers; OpenRouter needs no warmup."""
        if self.settings.provider.mode == "openrouter":
            log.info("provider is openrouter; local llama-server stays off")
            return
        try:
            await self.nora.server.ensure_running()
            log.info("model warm and ready")
        except Exception:
            log.exception("model failed to start; will retry on first message")

    async def stop_local_model(self) -> None:
        """Free the GPU when switching to hosted models."""
        if self.settings.provider.mode != "openrouter":
            return
        try:
            await self.nora.server.stop()
            log.info("local llama-server stopped for OpenRouter")
        except Exception:
            log.warning("could not stop llama-server", exc_info=True)

    async def close(self) -> None:
        log.info("shutting down")
        if self._tray is not None:
            self._tray.stop()
        current = asyncio.current_task()
        if self._presence_task is not None and self._presence_task is not current:
            self._presence_task.cancel()
        if self._stop_task is not None and self._stop_task is not current:
            self._stop_task.cancel()
        if self._proactive_task is not None:
            self._proactive_task.cancel()
        for task in (self._reminder_task, self._greeting_task):
            if task is not None:
                task.cancel()
        for task in list(self._timer_tasks.values()):
            task.cancel()
        for task in list(self._reaction_tasks):
            task.cancel()
        await self.nora.close()
        await super().close()

    # -- messaging -------------------------------------------------------
    async def on_message(self, message: discord.Message) -> None:
        if self.user and message.author.id == self.user.id:
            return
        if message.author.bot:
            return

        text = self._clean(message)
        images = await self._images(message)
        audio = await self._voice_notes(message)

        addressed = self._is_addressed(message)
        in_channel = self._in_allowlist(message.channel)
        if not addressed and not in_channel:
            return

        scope = self._scope(message)
        # he is back: she may ask where he went again another time
        self._followups.pop(scope, None)
        self._followup_at.pop(scope, None)
        if not self._user_allowed(message.author.id):
            log.debug("ignoring %s (not on allow_users)", message.author.id)
            return

        # Cheap freshness check: his own messages carry his current presence.
        if isinstance(message.author, discord.Member):
            self._remember_activity(message.author)

        if audio:
            voice_lock = self._voice_locks.setdefault(
                message.channel.id, asyncio.Lock()
            )
            async with voice_lock:
                transcript = await self._transcribe(message, audio)
            if transcript:
                text = f"{text}\n{transcript}".strip() if text else transcript
            elif not text:
                # Silent or unreadable clip: say so instead of guessing what he said.
                self.nora.store_for(scope).add_message(
                    scope,
                    "user",
                    "[sent a voice message]",
                    author_id=message.author.id,
                    author_name=message.author.display_name,
                )
                if addressed or (in_channel and self.settings.discord.listen_all):
                    fallback = "i couldnt hear that, say it again?"
                    try:
                        await self._send_chunk(message.channel, fallback)
                        self.nora.record_reply(scope, [fallback])
                    except Exception:
                        log.warning(
                            "could not send the voice fallback", exc_info=True
                        )
                return

        if not text and not images:
            text = self._attachment_note(message)
        if not text and not images:
            return

        # Silent on purpose: she just remembers, she does not answer yet.
        self._remember_events(scope, text)
        self._note_greeting(scope, text)

        voice_reply = (
            bool(audio)
            and self.settings.tts.enabled
            and self.settings.tts.reply_to_voice
        )
        if self._call_channel(message.channel) is not None:
            voice_reply = True

        if addressed or (in_channel and self.settings.discord.listen_all):
            self._schedule_reaction(message, scope, text)
            self._queue(message, text, images, voice_reply)
            return

        # Passive listening: keep it in memory, stay quiet.
        stored = text
        if images:
            stored = f"{text} {PHOTO_MARKER}".strip() if text else PHOTO_MARKER
        self.nora.store_for(scope).add_message(
            scope,
            "user",
            stored,
            author_id=message.author.id,
            author_name=message.author.display_name,
        )

    def _clean(self, message: discord.Message) -> str:
        text = _MENTION_RE.sub(" ", message.content or "")
        return _SPACE_RE.sub(" ", text).strip()

    @staticmethod
    def _attachment_note(message: discord.Message) -> str:
        names = [a.filename for a in message.attachments]
        if names:
            return f"[sent a file: {', '.join(names[:3])}]"
        if message.stickers:
            return f"[sent a sticker: {message.stickers[0].name}]"
        return ""

    @staticmethod
    async def _images(message: discord.Message) -> list[bytes]:
        blobs: list[bytes] = []
        for attachment in message.attachments:
            if len(blobs) >= MAX_IMAGES:
                break
            if attachment.size and attachment.size > MAX_IMAGE_BYTES:
                log.info("skipping oversized attachment %s", attachment.filename)
                continue
            content_type = (attachment.content_type or "").lower()
            suffix = Path(attachment.filename or "").suffix.lower()
            if not content_type.startswith("image/") and suffix not in IMAGE_SUFFIXES:
                continue
            try:
                blobs.append(await attachment.read())
            except Exception:
                log.warning("could not download an attachment", exc_info=True)
        return blobs

    async def _voice_notes(self, message: discord.Message) -> list[bytes]:
        cfg = self.settings.voice
        if not cfg.enabled:
            return []
        clips: list[bytes] = []
        for attachment in message.attachments:
            if len(clips) >= cfg.max_clips:
                break
            if not self._is_voice_attachment(attachment):
                continue
            if attachment.size and attachment.size > cfg.max_bytes:
                log.info("skipping oversized voice message %s", attachment.filename)
                continue
            try:
                clips.append(await attachment.read())
            except Exception:
                log.warning("could not download a voice message", exc_info=True)
        return clips

    @staticmethod
    def _is_voice_attachment(attachment: Any) -> bool:
        try:
            if attachment.is_voice_message():
                return True
        except Exception:
            pass
        content_type = (attachment.content_type or "").lower()
        suffix = Path(attachment.filename or "").suffix.lower()
        return content_type.startswith("audio/") or suffix in AUDIO_SUFFIXES

    async def _transcribe(
        self, message: discord.Message, clips: list[bytes]
    ) -> str:
        typing = getattr(message.channel, "typing", None)
        try:
            if typing is not None:
                async with typing():
                    return await asyncio.to_thread(
                        self.transcriber.transcribe_many, clips
                    )
            return await asyncio.to_thread(self.transcriber.transcribe_many, clips)
        except Exception:
            log.exception("voice transcription failed")
            return ""

    # -- reactions -------------------------------------------------------
    def _schedule_reaction(self, message: Any, scope: str, text: str) -> None:
        """React a little later, like she read it instead of firing instantly."""
        cfg = self.settings.reactions
        if not cfg.enabled:
            return
        if not self._is_farewell(text) and self._pick_reaction(text) is None:
            return
        low = max(0.0, float(cfg.delay_min))
        high = max(low, float(cfg.delay_max))
        delay = random.uniform(low, high)
        task = asyncio.create_task(self._react_later(message, scope, text, delay))
        self._reaction_tasks.add(task)
        task.add_done_callback(self._reaction_tasks.discard)

    async def _react_later(
        self, message: Any, scope: str, text: str, delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
            await self._maybe_react(message, scope, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("could not react to a message", exc_info=True)

    async def _maybe_react(self, message: Any, scope: str, text: str) -> None:
        cfg = self.settings.reactions
        if not cfg.enabled:
            return
        if self._is_farewell(text):
            # she always hearts a goodbye, even if it is minutes after the last one
            try:
                await message.add_reaction("❤️")
            except Exception:
                log.warning("could not react to a message", exc_info=True)
            return
        emoji = self._pick_reaction(text)
        if emoji is None:
            return
        chance = max(0.0, min(1.0, float(cfg.chance)))
        if chance <= 0 or random.random() >= chance:
            return
        now = datetime.now()
        last = self._last_reaction.get(scope)
        if last is not None and (now - last) < timedelta(
            minutes=max(0.0, float(cfg.cooldown_minutes))
        ):
            return
        try:
            await message.add_reaction(emoji)
        except Exception:
            log.warning("could not react to a message", exc_info=True)
            return
        self._last_reaction[scope] = now

    @staticmethod
    def _is_farewell(text: str) -> bool:
        lowered = " ".join((text or "").lower().split())
        return any(_cue_in(phrase, lowered) for phrase in FAREWELL_CUES)

    @staticmethod
    def _pick_reaction(text: str) -> str | None:
        """Return an emoji only when the message clearly carries that tone.

        None means there is no obvious cue, so she leaves it alone instead of
        picking something at random.
        """
        lowered = " ".join((text or "").lower().split())
        if not lowered:
            return None
        for emoji, phrases in REACTION_CUES:
            if any(_cue_in(phrase, lowered) for phrase in phrases):
                return emoji
        return None

    @staticmethod
    def _reaction_note(emoji: str, content: str) -> str:
        snippet = " ".join((content or "").split())[:120]
        return f'[reacted {emoji} to your message: "{snippet}"]'

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        cfg = self.settings.reactions
        if not cfg.enabled or not cfg.remember_user_reactions:
            return
        if self.user is not None and payload.user_id == self.user.id:
            return
        member = getattr(payload, "member", None)
        if member is not None and getattr(member, "bot", False):
            return
        try:
            channel = self.get_channel(payload.channel_id) or await self.fetch_channel(
                payload.channel_id
            )
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            log.debug("could not fetch a reacted message", exc_info=True)
            return
        if self.user is None or message.author.id != self.user.id:
            return
        scope = f"u:{payload.user_id}"
        note = self._reaction_note(str(payload.emoji), message.content or "")
        store = self.nora.store_for(scope)
        if any(m["content"] == note for m in store.recent(scope, 3)):
            return
        name = getattr(member, "display_name", None)
        store.add_message(
            scope,
            "user",
            note,
            author_id=payload.user_id,
            author_name=name,
        )
        log.info("[%s] saw a %s reaction", scope, payload.emoji)

    # -- voice output ----------------------------------------------------
    def _call_channel(self, channel: Any) -> Any:
        client = self._voice_client
        if client is None or not getattr(client, "is_connected", lambda: False)():
            return None
        voice_channel = getattr(client, "channel", None)
        if voice_channel is None:
            return None
        # Only the voice channel's own text chat turns typed messages into
        # speech, so replies from unrelated channels are not played aloud.
        if getattr(channel, "id", None) != getattr(voice_channel, "id", None):
            return None
        return voice_channel

    async def _send_voice_clip(self, channel: Any, speech: Speech) -> None:
        token = self.settings.discord.token
        if not token:
            raise RuntimeError("DISCORD_TOKEN is missing")
        payload = {
            "flags": discord.MessageFlags.voice.flag,
            "attachments": [
                {
                    "id": "0",
                    "filename": "voice-message.ogg",
                    "duration_secs": round(float(speech.duration), 2),
                    "waveform": base64.b64encode(speech.waveform).decode("ascii"),
                }
            ],
        }
        url = f"https://discord.com/api/v10/channels/{channel.id}/messages"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                for _ in range(2):
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bot {token}"},
                        data={"payload_json": json.dumps(payload)},
                        files={
                            "files[0]": (
                                "voice-message.ogg",
                                speech.opus,
                                "audio/ogg",
                            )
                        },
                    )
                    if response.status_code == 429:
                        wait = float(response.json().get("retry_after", 1.0))
                        await asyncio.sleep(min(wait, 5.0))
                        continue
                    if response.status_code >= 400:
                        raise RuntimeError(
                            "discord rejected the voice message: "
                            f"{response.status_code} {response.text[:200]}"
                        )
                    return
            raise RuntimeError("voice message rate limited")
        except Exception:
            log.warning(
                "voice message upload failed, sending an audio file instead",
                exc_info=True,
            )
            await channel.send(
                file=discord.File(
                    io.BytesIO(speech.opus), filename="nora-voice.ogg"
                )
            )

    async def _speak_in_call(self, text: str) -> bool:
        speech = await asyncio.to_thread(self.speaker.speak, text)
        if speech is None:
            return False
        return await self._play_in_call(speech)

    async def _play_in_call(self, speech: Speech) -> bool:
        client = self._voice_client
        if client is None or not getattr(client, "is_connected", lambda: False)():
            return False
        if self._voice_speak_lock is None:
            self._voice_speak_lock = asyncio.Lock()
        pcm = pcm_48k_stereo(speech.samples, speech.sample_rate)
        async with self._voice_speak_lock:
            while client.is_playing():
                await asyncio.sleep(0.1)
            client.play(discord.PCMAudio(io.BytesIO(pcm)))
            while client.is_playing():
                await asyncio.sleep(0.05)
        return True

    async def _join_voice(self, channel: Any) -> bool:
        cfg = self.settings.tts
        if not cfg.enabled or not cfg.voice_channel:
            return False
        was_here = (
            self._voice_client is not None
            and getattr(self._voice_client, "channel", None) == channel
        )
        try:
            client = channel.guild.voice_client
            if client is None:
                client = await channel.connect()
            elif getattr(client, "channel", None) != channel:
                await client.move_to(channel)
            self._voice_client = client
            self._voice_channel_id = channel.id
            if not was_here:
                log.info("joined voice channel %s", channel.id)
                if cfg.greeting:
                    await self._speak_in_call(cfg.greeting)
            return True
        except Exception:
            log.exception("could not join the voice channel")
            return False

    async def on_voice_state_update(
        self, member: Any, before: Any, after: Any
    ) -> None:
        if self.user is None or getattr(member, "id", None) != self.user.id:
            return
        if after.channel is None:
            self._voice_client = None
            self._voice_channel_id = None
            return
        if after.channel != before.channel:
            await self._join_voice(after.channel)

    async def _cmd_call(self, interaction: discord.Interaction) -> None:
        voice = getattr(interaction.user, "voice", None)
        channel = getattr(voice, "channel", None)
        if channel is None:
            await interaction.response.send_message(
                "join a voice channel first", ephemeral=True
            )
            return
        if not self.settings.tts.enabled:
            await interaction.response.send_message(
                "voice replies are turned off", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        joined = await self._join_voice(channel)
        message = "ok im here" if joined else "i couldnt join, check my permissions"
        await interaction.followup.send(message, ephemeral=True)

    async def _cmd_leave(self, interaction: discord.Interaction) -> None:
        client = self._voice_client
        if client is None:
            client = getattr(interaction.guild, "voice_client", None)
        if client is not None:
            await client.disconnect()
        self._voice_client = None
        self._voice_channel_id = None
        await interaction.response.send_message("left the call", ephemeral=True)

    def _user_allowed(self, user_id: int) -> bool:
        allowed = self.settings.discord.allow_users
        return not allowed or user_id in allowed

    def _is_addressed(self, message: discord.Message) -> bool:
        if message.guild is None:
            return self.settings.discord.respond_to_dms
        if message.reference is not None:
            resolved = message.reference.resolved
            if isinstance(resolved, discord.Message) and self.user:
                if resolved.author.id == self.user.id:
                    return True
        if self.user and self.user in message.mentions:
            return self.settings.discord.respond_to_mentions
        return False

    def _in_allowlist(self, channel: Any) -> bool:
        ids = set(self.settings.discord.channels)
        if not ids:
            return False
        cid = getattr(channel, "id", None)
        if cid in ids:
            return True
        parent = getattr(channel, "parent_id", None)
        return parent in ids

    @staticmethod
    def _scope(message: discord.Message) -> str:
        # One thread and one memory per person, wherever they talk to her.
        return f"u:{message.author.id}"

    # -- debounce + delivery --------------------------------------------
    def _queue(
        self,
        message: discord.Message,
        text: str,
        images: list[bytes],
        voice: bool = False,
    ) -> None:
        cid = message.channel.id
        if not self._pending.get(cid):
            # start of a burst: her think time is counted from here
            self._pending_since[cid] = time.monotonic()
        self._pending.setdefault(cid, []).append(
            (text, message.author.id, message.author.display_name, images, voice)
        )
        # A version token lets a newer message supersede the pending timer
        # without cancelling a reply that is already being generated.
        version = self._versions.get(cid, 0) + 1
        self._versions[cid] = version
        self._timer_tasks[cid] = asyncio.create_task(
            self._flush(message.channel, cid, version)
        )

    async def _flush(self, channel: Any, cid: int, version: int) -> None:
        started = self._pending_since.get(cid)
        burst_age = (time.monotonic() - started) if started is not None else 0.0
        wait = pacing.compute_wait(
            self.settings.chat.reply_delay, burst_age=burst_age
        )
        if wait:
            log.info("thinking for %.0fs before replying", wait)
        await asyncio.sleep(self.settings.chat.debounce + wait)
        if self._versions.get(cid) != version:
            return  # superseded; the newer message restarts her think time
        self._timer_tasks.pop(cid, None)
        self._pending_since.pop(cid, None)
        entries = self._pending.pop(cid, [])
        if not entries:
            return
        await self._respond(channel, cid, entries)

    async def _respond(self, channel: Any, cid: int, entries: Pending) -> None:
        """Answer each speaker separately, in the order they texted."""
        groups: list[Pending] = []
        for entry in entries:
            if groups and groups[-1][-1][1] == entry[1]:
                groups[-1].append(entry)
            else:
                groups.append([entry])

        lock = self._locks.setdefault(cid, asyncio.Lock())
        async with lock:
            for group in groups:
                await self._answer_group(channel, cid, group)

    async def _answer_group(
        self, channel: Any, cid: int, entries: Pending
    ) -> None:
        author_id, author_name = entries[-1][1], entries[-1][2]
        scope = f"u:{author_id}" if author_id is not None else f"dm:{cid}"
        text = "\n".join(e[0] for e in entries).strip()
        images = [blob for entry in entries for blob in entry[3]][:MAX_IMAGES]
        voice_reply = any(entry[4] for entry in entries)

        idle = self.nora.store_for(scope).user_idle_minutes(scope)
        return_delay = self._return_delay(idle)
        wake_delay = self._wake_delay(scope)
        delay = max(return_delay, wake_delay)
        if delay:
            log.info(
                "first reply delayed %.1fs (away %.1fh, wake delay %.1fs)",
                delay,
                (idle or 0.0) / 60.0,
                wake_delay,
            )
            await asyncio.sleep(delay)

        adaptations = 0
        delivered: list[str] = []

        while True:
            version = self._versions.get(cid, 0)
            try:
                async with channel.typing():
                    chunks = await self.nora.compose(
                        scope,
                        text,
                        author_id=author_id,
                        author_name=author_name,
                        images=images or None,
                        label_speakers=getattr(channel, "guild", None) is not None,
                        adjust=adaptations > 0,
                        doing=self._owner_doing(scope),
                    )
            except Exception:
                log.exception("failed to generate a reply")
                if not delivered:
                    try:
                        await channel.send("...my head hurts ... say that again?")
                    except Exception:
                        pass
                self.nora.record_reply(scope, delivered)
                return

            # he said something new while she was still composing
            pace = self.nora.mood_pace(scope)
            newer = self._take_pending_for(cid, author_id)
            if newer and adaptations < self.MAX_ADAPTATIONS:
                adaptations += 1
                text, images, voice_reply = self._merge_entries(
                    newer, images, voice_reply
                )
                log.info("he kept typing, adjusting her reply (%s)", scope)
                await self._rethink_pause()
                continue

            if voice_reply and chunks:
                speech = await asyncio.to_thread(
                    self.speaker.speak_chunks, chunks
                )
                if speech is not None:
                    if self._versions.get(cid, 0) != version:
                        newer = self._take_pending_for(cid, author_id)
                        if newer and adaptations < self.MAX_ADAPTATIONS:
                            adaptations += 1
                            text, images, voice_reply = self._merge_entries(
                                newer, images, voice_reply
                            )
                            await self._rethink_pause()
                            continue
                        break
                    try:
                        call = self._call_channel(channel)
                        if call is not None:
                            await self._play_in_call(speech)
                        else:
                            async with channel.typing():
                                await self._send_voice_clip(channel, speech)
                        delivered.extend(chunks)
                        break
                    except Exception:
                        log.exception(
                            "voice reply failed; falling back to text"
                        )
                else:
                    log.warning("TTS produced no audio; sending text instead")

            interrupted = False
            sent_bubbles = 0
            for chunk in chunks:
                if self._versions.get(cid, 0) != version:
                    interrupted = True
                    break
                if sent_bubbles:
                    gap = self._bubble_gap()
                    if gap:
                        await asyncio.sleep(gap)
                await self._maybe_tease_typing(channel)
                try:
                    sent, final_text = await self._send_bubble(
                        channel, chunk, pace=pace
                    )
                    delivered.append(final_text)
                except Exception:
                    log.exception("failed to send message")
                    interrupted = True
                    break
                sent_bubbles += 1

            if not interrupted:
                break

            newer = self._take_pending_for(cid, author_id)
            if not newer or adaptations >= self.MAX_ADAPTATIONS:
                break
            adaptations += 1
            text, images, voice_reply = self._merge_entries(
                newer, images, voice_reply
            )
            log.info("he talked over her, dropping the rest of the burst")
            await self._rethink_pause()

        self.nora.record_reply(scope, delivered)
        if delivered:
            log.info("[%s] <- %d text(s)", scope, len(delivered))

    def _return_delay(self, idle_minutes: float | None) -> float:
        cfg = self.settings.chat
        if idle_minutes is None or idle_minutes < cfg.long_gap_minutes:
            return 0.0
        low = max(0.0, float(cfg.return_delay_min))
        high = max(low, float(cfg.return_delay_max))
        return random.uniform(low, high)

    def _wake_delay(self, scope: str) -> float:
        cfg = self.settings.chat
        now = datetime.now()
        if not presence.just_woke(
            now, self.settings.presence.sleep_hours, cfg.waking_minutes
        ):
            return 0.0
        today = now.date()
        if self._wake_delay_used.get(scope) == today:
            return 0.0
        self._wake_delay_used[scope] = today
        low = max(0.0, float(cfg.wake_delay_min))
        high = max(low, float(cfg.wake_delay_max))
        return random.uniform(low, high)

    def _rethink_delay(self) -> float:
        cfg = self.settings.chat
        low = max(0.0, float(cfg.rethink_delay_min))
        high = max(low, float(cfg.rethink_delay_max))
        return random.uniform(low, high)

    async def _rethink_pause(self) -> None:
        delay = self._rethink_delay()
        if delay <= 0:
            return
        log.info("pausing %.1fs before typing the new reply", delay)
        await asyncio.sleep(delay)

    @staticmethod
    def _merge_entries(
        entries: Pending,
        fallback_images: list[bytes],
        fallback_voice: bool = False,
    ) -> tuple[str, list[bytes], bool]:
        text = "\n".join(e[0] for e in entries).strip()
        images = [blob for entry in entries for blob in entry[3]][:MAX_IMAGES]
        voice = fallback_voice or any(e[4] for e in entries)
        return text, (images or fallback_images), voice

    def _take_pending_for(self, cid: int, author_id: Any) -> Pending:
        """Pull only this person's queued messages, leaving others alone."""
        pending = self._pending.get(cid) or []
        mine = [entry for entry in pending if entry[1] == author_id]
        if not mine:
            return []
        self._pending[cid] = [e for e in pending if e[1] != author_id]
        return mine

    def _bubble_gap(self) -> float:
        cfg = self.settings.chat
        low = max(0.0, float(cfg.bubble_gap_min))
        high = max(low, float(cfg.bubble_gap_max))
        return random.uniform(low, high)

    async def _maybe_tease_typing(self, channel: Any) -> None:
        """Occasionally start typing, then stop without sending anything."""
        cfg = self.settings.chat
        chance = max(0.0, min(1.0, float(cfg.typing_tease_chance)))
        if chance <= 0 or random.random() >= chance:
            return
        low = max(0.0, float(cfg.typing_tease_min))
        high = max(low, float(cfg.typing_tease_max))
        async with channel.typing():
            await asyncio.sleep(random.uniform(low, high))
        await asyncio.sleep(random.uniform(0.15, 0.5))

    def _typo_variant(self, text: str) -> tuple[str, str | None]:
        """Return (text_to_send, corrected_text) for a rare self-correction."""
        cfg = self.settings.chat
        chance = max(0.0, min(1.0, float(cfg.self_correct_chance)))
        if chance <= 0 or random.random() >= chance:
            return text, None
        matches = list(re.finditer(r"[A-Za-z]{5,}", text))
        if not matches:
            return text, None
        for _ in range(8):
            match = random.choice(matches)
            word = match.group(0)
            indexes = [
                index
                for index in range(1, len(word) - 2)
                if word[index] != word[index + 1]
            ]
            if not indexes:
                continue
            index = random.choice(indexes)
            typo = word[:index] + word[index + 1] + word[index] + word[index + 2:]
            if typo != word:
                return text[: match.start()] + typo + text[match.end():], text
        return text, None

    async def _send_bubble(
        self, channel: Any, chunk: str, *, pace: float = 1.0
    ) -> tuple[Any, str]:
        """Send one bubble, with an optional quick correction edit."""
        send_text, correction = self._typo_variant(chunk)
        sent = await self._send_chunk(channel, send_text, pace=pace)
        if correction and sent is not None:
            delay = random.uniform(
                self.settings.chat.self_correct_delay_min,
                self.settings.chat.self_correct_delay_max,
            )
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await sent.edit(content=correction)
            except Exception:
                log.warning("could not correct a typo", exc_info=True)
        return sent, (correction or send_text)

    def _think_pause(self) -> float:
        """How long she stops mid-reply to think, or 0 for no pause."""
        cfg = self.settings.chat
        chance = max(0.0, min(1.0, float(cfg.think_pause_chance)))
        if chance <= 0 or random.random() >= chance:
            return 0.0
        low = max(0.0, float(cfg.think_pause_min))
        high = max(low, float(cfg.think_pause_max))
        return random.uniform(low, high)

    async def _send_chunk(
        self, channel: Any, chunk: str, *, pace: float = 1.0
    ) -> Any | None:
        cfg = self.settings.chat
        delay = typing_seconds(
            chunk,
            cps=cfg.typing_cps,
            minimum=cfg.min_delay,
            maximum=cfg.max_delay,
        )
        delay *= max(0.5, min(2.0, float(pace)))
        pause = self._think_pause()
        if pause <= 0:
            async with channel.typing():
                await asyncio.sleep(delay)
                return await channel.send(chunk)

        # Type for a bit, let the indicator drop while she thinks, then finish.
        typed = delay * random.uniform(0.3, 0.7)
        async with channel.typing():
            await asyncio.sleep(typed)
        log.info("stopping to think for %.1fs mid-reply", pause)
        await asyncio.sleep(pause)
        async with channel.typing():
            await asyncio.sleep(max(0.05, delay - typed))
            return await channel.send(chunk)

    async def _deliver(self, channel: Any, chunks: list[str]) -> None:
        """Type for roughly as long as each bubble is, then send it."""
        for index, chunk in enumerate(chunks):
            if index:
                gap = self._bubble_gap()
                if gap:
                    await asyncio.sleep(gap)
            await self._maybe_tease_typing(channel)
            await self._send_bubble(channel, chunk)

    # -- presence --------------------------------------------------------
    async def _apply_presence(self) -> None:
        """Show online-on-her-phone, idle while asleep, dnd when busy."""
        cfg = self.settings.presence
        state = presence.compute(datetime.now(), cfg)
        status = {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
        }.get(state.status, discord.Status.online)

        activity = None
        if state.activity:
            activity = discord.CustomActivity(
                name=state.activity, emoji=state.emoji or None
            )
        key = (state.status, state.activity)
        if key != self._presence_key:
            self._presence_key = key
            log.info(
                "presence -> %s (%s)", state.status, state.activity or "no activity"
            )
        try:
            await self.change_presence(status=status, activity=activity)
        except Exception:
            log.warning("custom status rejected, falling back", exc_info=True)
            if state.activity:
                try:
                    await self.change_presence(
                        status=status, activity=discord.Game(name=state.activity)
                    )
                except Exception:
                    log.warning("could not set presence", exc_info=True)

    async def _presence_loop(self) -> None:
        cfg = self.settings.presence
        interval = max(60.0, float(cfg.refresh_minutes) * 60.0)
        while True:
            try:
                await self._apply_presence()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("presence refresh failed")
            await asyncio.sleep(interval)

    # -- what he is doing ------------------------------------------------
    def _remember_activity(self, member: Any) -> None:
        """Cache the owner's status. Nothing about anyone else is stored."""
        if not self._track_owner or member is None:
            return
        owner = getattr(self.nora, "owner_id", None)
        if owner is None or getattr(member, "id", None) != owner:
            return
        try:
            line = activity_mod.describe_member(member)
        except Exception:
            log.debug("could not read his presence", exc_info=True)
            return
        if line != self._owner_activity:
            self._owner_activity = line
            log.info("he is now: %s", line or "unknown")

    def _scan_owner_presence(self) -> None:
        """Pick up his status once on login, before any update arrives."""
        owner = getattr(self.nora, "owner_id", None)
        if not self._track_owner or owner is None:
            return
        for guild in self.guilds:
            member = guild.get_member(owner)
            if member is not None:
                self._remember_activity(member)
                return

    async def on_presence_update(self, before: Any, after: Any) -> None:
        self._remember_activity(after)

    def _owner_doing(self, scope: str) -> str | None:
        """Only her owner's activity ever reaches a prompt."""
        if not self._track_owner or not self._owner_activity:
            return None
        is_owner = getattr(self.nora, "is_owner", None)
        if callable(is_owner) and not is_owner(scope):
            return None
        return self._owner_activity

    # -- texting first ---------------------------------------------------
    def _start_proactive(self) -> None:
        cfg = self.settings.proactive
        if not cfg.user_id:
            log.info("proactive texting needs proactive.user_id")
            return
        if self._proactive_task is not None and not self._proactive_task.done():
            return
        if not cfg.enabled:
            log.info("proactive texting armed but paused")
        self._proactive_task = asyncio.create_task(self._proactive_loop())

    def _target_uid(self) -> int | None:
        """Who she DMs on her own: her owner, else proactive.user_id."""
        owner = getattr(self.nora, "owner_id", None)
        return owner or self.settings.proactive.user_id

    async def _proactive_channel(self) -> tuple[Any, str]:
        if self._direct_channel is not None:
            return self._direct_channel, f"u:{self._proactive_uid}"
        uid = self._target_uid()
        if not uid:
            raise RuntimeError("no persona.owner_id or proactive.user_id configured")
        user = self.get_user(uid) or await self.fetch_user(uid)
        dm = user.dm_channel or await user.create_dm()
        self._direct_channel = dm
        self._proactive_uid = uid
        log.info("proactive target dm: %s", dm.id)
        return dm, f"u:{uid}"

    def _last_activity(self, scope: str) -> datetime | None:
        idle = self.nora.store_for(scope).idle_minutes(scope)
        if idle is None:
            return None
        return datetime.now() - timedelta(minutes=idle)

    def _unanswered_streak(self, scope: str) -> int:
        streak = 0
        for message in reversed(self.nora.store_for(scope).recent(scope, 8)):
            if message["role"] != "assistant":
                break
            streak += 1
        return streak

    async def _watch_stop_file(self) -> None:
        """Exit cleanly when scripts/stop.ps1 asks.

        The file is left in place on purpose: the supervisor reads it to decide
        whether to restart the bot or shut down for good.
        """
        path = self.settings.path("data/stop")
        while True:
            await asyncio.sleep(2)
            if path.exists():
                log.info("stop requested, shutting down cleanly")
                await self.close()
                return

    def _reset_daily_counter(self, now: datetime) -> None:
        if self._sent_day != now.date():
            self._sent_day = now.date()
            self._sent_today = 0

    async def _proactive_loop(self) -> None:
        cfg = self.settings.proactive
        log.info("proactive texting armed for user %s", cfg.user_id)
        now = datetime.now()
        self._reset_daily_counter(now)
        self._next_proactive_at = now + timedelta(
            seconds=proactive_mod.first_delay_seconds(cfg)
        )

        while True:
            try:
                await asyncio.sleep(30)
                now = datetime.now()
                self._reset_daily_counter(now)

                try:
                    channel, scope = await self._proactive_channel()
                except Exception:
                    log.exception("could not open her dm channel")
                    self._next_proactive_at = now + timedelta(minutes=15)
                    continue

                # checked every tick, not just when a spontaneous text is due
                if await self._maybe_follow_up(channel, scope, now):
                    self._next_proactive_at = now + timedelta(
                        seconds=proactive_mod.next_gap_seconds(cfg)
                    )
                    continue

                if (
                    self._next_proactive_at is not None
                    and now < self._next_proactive_at
                ):
                    continue

                decision = proactive_mod.decide(
                    now,
                    last_activity=self._last_activity(scope),
                    last_proactive=self._last_proactive,
                    sent_today=self._sent_today,
                    unanswered=self._unanswered_streak(scope),
                    cfg=cfg,
                    roll=random.random(),
                )
                log.debug("proactive check: %s", decision.reason)

                if decision.send and await self._send_proactive(channel, scope):
                    self._sent_today += 1
                    self._last_proactive = now
                    log.info("sent a spontaneous text (%d today)", self._sent_today)

                self._next_proactive_at = now + timedelta(
                    seconds=self._next_attempt(now, decision, cfg)
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("proactive loop error")
                await asyncio.sleep(60)

    @staticmethod
    def _next_attempt(
        now: datetime, decision: proactive_mod.Decision, cfg: ProactiveConfig
    ) -> float:
        """Do not waste a timer slot inside a window that can never send."""
        if decision.reason == "quiet hours":
            target = proactive_mod.quiet_end(now, cfg.quiet_hours) + timedelta(
                minutes=random.uniform(5, 40)
            )
            return max(60.0, (target - now).total_seconds())
        if decision.reason == "daily limit reached":
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                days=1
            )
            target = midnight + timedelta(minutes=random.uniform(10, 60))
            return max(60.0, (target - now).total_seconds())
        return proactive_mod.next_gap_seconds(cfg)

    async def _maybe_follow_up(
        self, channel: Any, scope: str, now: datetime
    ) -> bool:
        """Ask where he went when the thread went quiet on his side."""
        cfg = self.settings.proactive
        store = self.nora.store_for(scope)
        recent = store.recent(scope, 1)
        last_user_text = None
        for message in reversed(store.recent(scope, 10)):
            if message["role"] == "user":
                last_user_text = message["content"]
                break
        decision = proactive_mod.should_follow_up(
            now,
            idle_minutes=store.idle_minutes(scope),
            last_role=recent[0]["role"] if recent else None,
            followups_sent=self._followups.get(scope, 0),
            sent_today=self._sent_today,
            cfg=cfg,
            last_user_text=last_user_text,
            minutes_since_followup=(
                (now - self._followup_at[scope]).total_seconds() / 60.0
                if scope in self._followup_at
                else None
            ),
        )
        if not decision.send:
            log.debug("follow up check: %s", decision.reason)
            return False

        # Do not worry at 4am if that is when he is normally asleep.
        window = store.quiet_window(scope)
        if window and proactive_mod.in_quiet_hours(now, window):
            log.debug("follow up skipped: his usual quiet hours")
            return False

        worry_level = self._followups.get(scope, 0) + 1
        lock = self._locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            try:
                chunks = await self.nora.initiate(
                    scope,
                    follow_up=True,
                    worry_level=worry_level,
                    doing=self._owner_doing(scope),
                )
            except Exception:
                log.exception("could not compose a follow up")
                return False
            if not chunks:
                return False
            try:
                await self._deliver(channel, chunks)
            except Exception:
                log.exception("failed to send a follow up")
                return False

        self._followups[scope] = self._followups.get(scope, 0) + 1
        self._followup_at[scope] = now
        log.info(
            "follow up %d sent to %s", self._followups[scope], scope
        )
        return True

    async def _send_proactive(self, channel: Any, scope: str) -> bool:
        lock = self._locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            try:
                chunks = await self.nora.initiate(
                    scope, doing=self._owner_doing(scope)
                )
            except Exception:
                log.exception("could not compose a spontaneous text")
                return False
            if not chunks:
                log.info("she had nothing to say, skipping")
                return False
            try:
                await self._deliver(channel, chunks)
            except Exception:
                log.exception("failed to send a spontaneous text")
                return False
            return True

    # -- reminders and greetings -----------------------------------------
    def _start_scheduler(self) -> None:
        """Reminders and greetings have their own clock, not the proactive one."""
        if self._target_uid() is None:
            log.info(
                "reminders and greetings need persona.owner_id or proactive.user_id"
            )
            return
        if self.settings.reminders.enabled and (
            self._reminder_task is None or self._reminder_task.done()
        ):
            self._reminder_task = asyncio.create_task(self._reminders_loop())
        if self.settings.greetings.enabled and (
            self._greeting_task is None or self._greeting_task.done()
        ):
            self._greeting_task = asyncio.create_task(self._greetings_loop())

    def _remember_events(self, scope: str, text: str) -> None:
        """Store whatever he just said about his week. She stays quiet for now."""
        cfg = self.settings.reminders
        if not cfg.enabled or not text:
            return
        # Only her owner gets scheduled texts, so only his events matter.
        is_owner = getattr(self.nora, "is_owner", None)
        if callable(is_owner) and not is_owner(scope):
            return
        try:
            item = reminders_mod.remember(
                self.nora.store_for(scope), text, datetime.now(), cfg
            )
        except Exception:
            log.debug("could not read an event out of that", exc_info=True)
            return
        if item is not None:
            log.info("noted %s at %s for %s", item.title, item.when, scope)

    def _note_greeting(
        self, scope: str, text: str, now: datetime | None = None
    ) -> None:
        """Learn when he says good morning and goodnight, and move her windows."""
        cfg = self.settings.greetings
        if not cfg.enabled or not text:
            return
        is_owner = getattr(self.nora, "is_owner", None)
        if callable(is_owner) and not is_owner(scope):
            return
        store = self.nora.store_for(scope)
        try:
            noted = greetings_mod.note_user_greeting(store, text, now or datetime.now())
        except Exception:
            log.debug("could not read a greeting out of that", exc_info=True)
            return
        if noted is None:
            return
        kind, count = noted
        profile = greetings_mod.load_profile(store)
        log.info(
            "he said %s (%d samples). windows: %s",
            kind,
            count,
            greetings_mod.describe_windows(cfg, profile, self._greeting_seed),
        )

    async def _send_scheduled(
        self,
        channel: Any,
        scope: str,
        *,
        kind: str,
        note: str | None,
        fallback: str,
    ) -> bool:
        """Compose and deliver one greeting or reminder, with a template fallback."""
        lock = self._locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            try:
                chunks = await self.nora.initiate(
                    scope,
                    occasion=kind,
                    occasion_note=note,
                    doing=self._owner_doing(scope),
                )
            except Exception:
                log.exception("could not compose the %s text", kind)
                chunks = []
            if not chunks:
                chunks = [fallback]
            try:
                await self._deliver(channel, chunks)
            except Exception:
                log.exception("failed to send the %s text", kind)
                return False
        log.info("sent the %s text", kind)
        return True

    async def _reminders_loop(self) -> None:
        log.info("reminders armed")
        while True:
            try:
                await asyncio.sleep(30)
                await self._check_reminders()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reminder check failed")
                await asyncio.sleep(60)

    async def _check_reminders(self, now: datetime | None = None) -> bool:
        """Send a reminder if one is due. True when something went out."""
        now = now or datetime.now()
        try:
            channel, scope = await self._proactive_channel()
        except Exception:
            log.debug("no dm open yet; reminders wait")
            return False
        store = self.nora.store_for(scope)
        items = reminders_mod.load(store)
        if not items:
            return False
        kept = reminders_mod.prune(items, now)
        if len(kept) != len(items):
            reminders_mod.save(store, kept)
        batch = reminders_mod.due(kept, now)
        if not batch:
            return False
        if proactive_mod.in_quiet_hours(now, self.settings.proactive.quiet_hours):
            log.info("a reminder is due but it is quiet hours")
            return False
        sent = await self._send_scheduled(
            channel,
            scope,
            kind=REMINDER_OCCASION,
            note=reminders_mod.describe_many(batch, now),
            fallback=reminders_mod.reminder_text(batch, now),
        )
        if not sent:
            return False
        for item in batch:
            item.notified_at = now.isoformat(timespec="seconds")
        reminders_mod.save(store, kept)
        return True

    async def _greetings_loop(self) -> None:
        log.info("greetings armed")
        cfg = self.settings.greetings
        uid = self._target_uid()
        if uid is not None:
            scope = f"u:{uid}"
            store = self.nora.store_for(scope)
            # Start from his actual sleep pattern in the transcript, so the
            # windows are not stuck on the defaults for the first few days.
            sleep_window = store.quiet_window(scope, hours=8)
            self._greeting_seed = greetings_mod.seed_from_sleep_window(sleep_window)
            log.info(
                "greeting windows: %s",
                greetings_mod.describe_windows(
                    cfg, greetings_mod.load_profile(store), self._greeting_seed
                ),
            )
        while True:
            try:
                await asyncio.sleep(45)
                await self._maybe_greet()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("greeting check failed")
                await asyncio.sleep(60)

    async def _maybe_greet(self, now: datetime | None = None) -> bool:
        cfg = self.settings.greetings
        now = now or datetime.now()
        try:
            channel, scope = await self._proactive_channel()
        except Exception:
            log.debug("no dm open yet; greetings wait")
            return False
        store = self.nora.store_for(scope)
        sent_log = greetings_mod.load_sent(store)
        profile = greetings_mod.load_profile(store)
        if self._unanswered_streak(scope) >= self.settings.proactive.max_unanswered:
            return False
        kind = greetings_mod.due(
            now,
            cfg,
            sent_log,
            profile=profile,
            seed=self._greeting_seed,
            chatted_minutes=store.idle_minutes(scope),
            roll=random.random(),
        )
        if kind is None:
            return False
        # No quiet-hours check here on purpose: the window itself is the
        # schedule. If his bedtime is 02:30, a goodnight at 02:30 is the whole
        # point, and proactive.quiet_hours would block exactly that.
        occasion = (
            MORNING_OCCASION if kind == greetings_mod.MORNING else NIGHT_OCCASION
        )
        ok = await self._send_scheduled(
            channel,
            scope,
            kind=occasion,
            note=None,
            fallback=greetings_mod.fallback(kind),
        )
        if ok:
            greetings_mod.mark(store, kind, now)
        return ok

    # -- tray controls ---------------------------------------------------
    def _start_tray(self) -> None:
        if not self.settings.tray.enabled:
            log.info("tray icon disabled in config")
            return
        if self._tray is not None:
            return
        try:
            from .tray import NoraTray
        except ImportError:
            log.warning(
                "tray icon needs pystray and pillow: pip install -r requirements.txt"
            )
            return
        try:
            self._tray = NoraTray(self, asyncio.get_running_loop())
            self._tray.start()
            log.info("tray icon is up")
        except Exception:
            log.exception("could not start the tray icon")
            self._tray = None

    def set_proactive_enabled(self, enabled: bool) -> None:
        """Called from the tray thread; a plain attribute write is enough.

        The loop keeps running and simply skips while paused, which avoids
        cancelling a task from another thread.
        """
        self.controls.set_proactive_enabled(enabled)

    async def text_me_now(self) -> bool:
        """Send one text right now, ignoring every scheduling guard."""
        try:
            channel, scope = await self._proactive_channel()
        except Exception:
            log.exception("cannot open her dm channel")
            return False
        if await self._send_proactive(channel, scope):
            self._last_proactive = datetime.now()
            log.info("sent an on-demand text")
            return True
        return False

    async def tray_status(self) -> dict[str, Any]:
        cfg = self.settings.proactive
        provider = self.settings.provider
        if provider.mode == "openrouter":
            model_ready = bool(
                (provider.openrouter.api_key or "").strip()
                and (provider.openrouter.model or "").strip()
            )
            provider_model = provider.openrouter.model
        else:
            try:
                model_ready = await asyncio.wait_for(
                    self.nora.server.healthy(), 2.0
                )
            except Exception:
                model_ready = False
            provider_model = "llama.cpp"

        now = datetime.now()
        next_in = None
        if self._next_proactive_at is not None:
            next_in = max(
                0, int((self._next_proactive_at - now).total_seconds() // 60)
            )
        return {
            "ready": bool(self.user and self.is_ready()),
            "model_ready": bool(model_ready),
            "provider": provider.mode,
            "provider_model": provider_model,
            "proactive_enabled": bool(cfg.enabled),
            "preset": self.controls.preset_name(),
            "sent_today": self._sent_today,
            "max_per_day": cfg.max_per_day,
            "next_in_minutes": next_in,
            "quiet": proactive_mod.in_quiet_hours(now, cfg.quiet_hours),
        }

    # -- slash commands --------------------------------------------------
    async def _cmd_recall(self, interaction: discord.Interaction, query: str) -> None:
        hits = self.nora.recall(query, limit=8, scope=f"u:{interaction.user.id}")
        if not hits:
            await interaction.response.send_message(
                "i got nothing on that ... weird", ephemeral=True
            )
            return
        lines = []
        for hit in hits:
            stamp = str(hit.get("created_at") or "")[:16].replace("T", " ")
            who = "nora" if hit["role"] == "assistant" else (hit.get("author_name") or "you")
            snippet = " ".join(str(hit["content"]).split())[:180]
            lines.append(f"`{stamp}` {who}: {snippet}")
        body = "**recall: " + query.replace("*", "") + "**\n" + "\n".join(lines)
        await interaction.response.send_message(body[:1950], ephemeral=True)

    async def _cmd_memory(self, interaction: discord.Interaction) -> None:
        facts = self.nora.store_for(f"u:{interaction.user.id}").facts(
            f"u:{interaction.user.id}", limit=60
        )
        if not facts:
            await interaction.response.send_message(
                "nothing in there yet ... tell me something worth keeping",
                ephemeral=True,
            )
            return
        lines = [f"`{f['id']:>3}` {f['content']}" for f in facts]
        body = "**what i remember**\n" + "\n".join(lines)
        await interaction.response.send_message(body[:1950], ephemeral=True)

    async def _cmd_forget(self, interaction: discord.Interaction, fact_id: int) -> None:
        ok = self.nora.store_for(f"u:{interaction.user.id}").forget_fact(
            fact_id, scope=f"u:{interaction.user.id}"
        )
        await interaction.response.send_message(
            f"forgot `{fact_id}`" if ok else f"there is no `{fact_id}`",
            ephemeral=True,
        )

    async def _cmd_reminders(self, interaction: discord.Interaction) -> None:
        scope = f"u:{interaction.user.id}"
        now = datetime.now()
        items = reminders_mod.pending(
            reminders_mod.load(self.nora.store_for(scope)), now
        )
        if not items:
            await interaction.response.send_message(
                "nothing coming up ... tell me when something is", ephemeral=True
            )
            return
        lines = [
            f"`{item.title}` {reminders_mod.format_when(item.at, now)}"
            for item in items[:12]
        ]
        await interaction.response.send_message(
            "**reminders**\n" + "\n".join(lines), ephemeral=True
        )

    async def _cmd_status(self, interaction: discord.Interaction) -> None:
        stats = self.nora.store_for(f"u:{interaction.user.id}").stats()
        try:
            model = self.nora.settings.resolve_model_path()
            model_name = model.name
        except Exception:
            model_name = "not downloaded"
        healthy = await self.nora.server.healthy()
        uptime = int(time.time() - self.started_at)
        hours, rem = divmod(uptime, 3600)
        minutes = rem // 60
        await interaction.response.send_message(
            f"**{self.nora.settings.persona.name}**\n"
            f"model: `{model_name}` {'(loaded)' if healthy else '(loading/failed)'}\n"
            f"up: {hours}h {minutes}m\n"
            f"messages: {stats['messages']}  facts: {stats['facts']}  "
            f"summaries: {stats['summaries']}",
            ephemeral=True,
        )


def run_bot(settings: Settings, nora: Nora) -> None:
    token = settings.discord.token
    if not token:
        raise SystemExit(
            "No DISCORD_TOKEN found. Copy .env.example to .env and paste your bot token."
        )
    try:
        NoraBot(settings, nora).run(token, log_handler=None)
        return
    except discord.PrivilegedIntentsRequired:
        if not settings.presence.track_owner:
            raise
        log.error(
            "Discord refused the presence intents (close code 4014). Enable "
            "Presence and Server Members under Bot -> Privileged Gateway "
            "Intents in the developer portal, or set presence.track_owner: false "
            "in config.yaml. Starting again without presence tracking."
        )
    NoraBot(settings, nora, privileged=False).run(token, log_handler=None)
