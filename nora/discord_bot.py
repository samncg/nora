"""Discord transport: debounced replies, typing delays, memory commands."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from . import pacing
from . import presence
from . import proactive as proactive_mod
from .brain import PHOTO_MARKER, Nora
from .config import ProactiveConfig, Settings
from .humanize import typing_seconds
from .runtime import RuntimeControls
from .vision import MAX_BYTES as MAX_IMAGE_BYTES
from .vision import MAX_IMAGES

log = logging.getLogger("nora.discord")

_MENTION_RE = re.compile(r"<@[!&]?\d+>")
_SPACE_RE = re.compile(r"[ \t]{2,}")

Pending = list[tuple[str, int | None, str | None, list[bytes]]]

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


class NoraBot(commands.Bot):
    # how many times she may rewrite herself within one burst
    MAX_ADAPTATIONS = 2
    def __init__(self, settings: Settings, nora: Nora):
        intents = discord.Intents.default()
        # Privileged intent: enable it in the Developer Portal too.
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

        self.settings = settings
        self.nora = nora
        self.started_at = time.time()
        self.controls = RuntimeControls(settings)
        self._pending: dict[int, Pending] = {}
        self._pending_since: dict[int, float] = {}
        self._timer_tasks: dict[int, asyncio.Task[None]] = {}
        self._versions: dict[int, int] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._proactive_task: asyncio.Task[None] | None = None
        self._presence_task: asyncio.Task[None] | None = None
        self._presence_key: tuple[str, str] | None = None
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
            # Icon first, so it is there while the model is still loading.
            self._start_tray()
            try:
                await self.nora.server.ensure_running()
                log.info("model warm and ready")
            except Exception:
                log.exception("model failed to start; will retry on first message")
            finally:
                self._start_proactive()
                self._stop_task = asyncio.create_task(self._watch_stop_file())

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
        for task in list(self._timer_tasks.values()):
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
        if not text and not images:
            text = self._attachment_note(message)
        if not text and not images:
            return

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

        if addressed or (in_channel and self.settings.discord.listen_all):
            self._queue(message, text, images)
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
        self, message: discord.Message, text: str, images: list[bytes]
    ) -> None:
        cid = message.channel.id
        if not self._pending.get(cid):
            # start of a burst: her think time is counted from here
            self._pending_since[cid] = time.monotonic()
        self._pending.setdefault(cid, []).append(
            (text, message.author.id, message.author.display_name, images)
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
            newer = self._take_pending_for(cid, author_id)
            if newer and adaptations < self.MAX_ADAPTATIONS:
                adaptations += 1
                text, images = self._merge_entries(newer, images)
                log.info("he kept typing, adjusting her reply (%s)", scope)
                continue

            interrupted = False
            for chunk in chunks:
                if self._versions.get(cid, 0) != version:
                    interrupted = True
                    break
                delivered.append(chunk)
                try:
                    await self._send_chunk(channel, chunk)
                except Exception:
                    log.exception("failed to send message")
                    interrupted = True
                    break

            if not interrupted:
                break

            newer = self._take_pending_for(cid, author_id)
            if not newer or adaptations >= self.MAX_ADAPTATIONS:
                break
            adaptations += 1
            text, images = self._merge_entries(newer, images)
            log.info("he talked over her, dropping the rest of the burst")

        self.nora.record_reply(scope, delivered)
        if delivered:
            log.info("[%s] <- %d text(s)", scope, len(delivered))

    @staticmethod
    def _merge_entries(
        entries: Pending, fallback_images: list[bytes]
    ) -> tuple[str, list[bytes]]:
        text = "\n".join(e[0] for e in entries).strip()
        images = [blob for entry in entries for blob in entry[3]][:MAX_IMAGES]
        return text, (images or fallback_images)

    def _take_pending_for(self, cid: int, author_id: Any) -> Pending:
        """Pull only this person's queued messages, leaving others alone."""
        pending = self._pending.get(cid) or []
        mine = [entry for entry in pending if entry[1] == author_id]
        if not mine:
            return []
        self._pending[cid] = [e for e in pending if e[1] != author_id]
        return mine

    async def _send_chunk(self, channel: Any, chunk: str) -> None:
        cfg = self.settings.chat
        delay = typing_seconds(
            chunk,
            cps=cfg.typing_cps,
            minimum=cfg.min_delay,
            maximum=cfg.max_delay,
        )
        async with channel.typing():
            await asyncio.sleep(delay)
            await channel.send(chunk)

    async def _deliver(self, channel: Any, chunks: list[str]) -> None:
        """Type for roughly as long as the bubble is, then send it."""
        for chunk in chunks:
            await self._send_chunk(channel, chunk)

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

    async def _proactive_channel(self) -> tuple[Any, str]:
        if self._direct_channel is not None:
            return self._direct_channel, f"u:{self._proactive_uid}"
        uid = self.settings.proactive.user_id
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
                    scope, follow_up=True, worry_level=worry_level
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
                chunks = await self.nora.initiate(scope)
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
        try:
            model_ready = await asyncio.wait_for(self.nora.server.healthy(), 2.0)
        except Exception:
            model_ready = False

        now = datetime.now()
        next_in = None
        if self._next_proactive_at is not None:
            next_in = max(
                0, int((self._next_proactive_at - now).total_seconds() // 60)
            )
        return {
            "ready": bool(self.user and self.is_ready()),
            "model_ready": bool(model_ready),
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
    bot = NoraBot(settings, nora)
    bot.run(token, log_handler=None)
