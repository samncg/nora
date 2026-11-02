"""The brain: prompt assembly, replies, and background memory upkeep."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import consolidate as consolidate_mod
from .academic import AcademicCalendar
from .config import Settings
from .humanize import estimate_tokens, split_messages
from .llm import LlamaClient, LlamaServer
from .memory import MemoryStore
from .persona import (
    FOLLOW_UP_CUE,
    FOLLOW_UP_WORRIED_CUE,
    PROACTIVE_CUE,
    build_system_prompt,
)
from .timetable import Timetable
from .vision import prepare_images, to_content_parts

log = logging.getLogger("nora.brain")

FALLBACKS = ["hm.", "...what", "ok wait say that again"]

# Kept in the stored transcript so she can remember that a picture happened,
# long after the image itself is gone from the prompt.
PHOTO_MARKER = "[sent a photo]"


def _clock(hour: int) -> str:
    hour = hour % 24
    suffix = "am" if hour < 12 else "pm"
    return f"{hour % 12 or 12}{suffix}"


class Nora:
    def __init__(
        self,
        settings: Settings,
        *,
        store: MemoryStore | None = None,
        server: LlamaServer | None = None,
        client: LlamaClient | None = None,
    ):
        self.settings = settings
        # Whose timetable and calendar these are.
        self.owner_id = settings.persona.owner_id or settings.proactive.user_id
        self._injected_store = store
        self._stores: dict[str, MemoryStore] = {}
        self.server = server or LlamaServer(settings)
        self.client = client or LlamaClient(settings, self.server)
        self._consolidating = False
        self.timetable = self._load_timetable()
        self.academic = self._load_calendar()

    # -- per-person memory ------------------------------------------------
    @staticmethod
    def _person_key(scope: str) -> str | None:
        """`u:1234` -> `1234`; anything else is not a person scope."""
        if not scope.startswith("u:"):
            return None
        raw = scope.split(":", 1)[1]
        clean = "".join(ch for ch in raw if ch.isalnum() or ch in "-_")
        return clean or None

    def store_for(self, scope: str) -> MemoryStore:
        """Each person gets their own sqlite file, so contexts cannot mix."""
        if self._injected_store is not None:
            return self._injected_store

        key = self._person_key(scope)
        cache_key = key or "_shared"
        store = self._stores.get(cache_key)
        if store is not None:
            return store

        if key is None:
            path = self.settings.path(self.settings.memory.db_path)
        else:
            directory = self.settings.path(self.settings.memory.dir)
            path = directory / f"{key}.db"
        store = MemoryStore(path)
        self._stores[cache_key] = store
        log.info("memory file for %s: %s", cache_key, store.path)
        return store

    @property
    def store(self) -> MemoryStore:
        """Her owner's memory. Kept for convenience and status output."""
        scope = f"u:{self.owner_id}" if self.owner_id else "_shared"
        return self.store_for(scope)

    def stats(self) -> dict[str, int]:
        """Totals across every memory file she has open right now."""
        totals = {"messages": 0, "facts": 0, "summaries": 0, "people": 0}
        for store in self._stores.values():
            counts = store.stats()
            totals["messages"] += counts["messages"]
            totals["facts"] += counts["facts"]
            totals["summaries"] += counts["summaries"]
        return totals

    def _load_timetable(self) -> Timetable | None:
        if not self.settings.timetable.enabled:
            return None
        try:
            return Timetable.load(self.settings.path(self.settings.timetable.file))
        except Exception:
            log.warning("could not load the timetable", exc_info=True)
            return None

    def _load_calendar(self) -> AcademicCalendar | None:
        if not self.settings.calendar.enabled:
            return None
        try:
            return AcademicCalendar.load(
                self.settings.path(self.settings.calendar.file)
            )
        except Exception:
            log.warning("could not load the calendar", exc_info=True)
            return None

    @property
    def vision_enabled(self) -> bool:
        return self.settings.resolve_mmproj_path() is not None

    def is_owner(self, scope: str) -> bool:
        return self.owner_id is not None and scope == f"u:{self.owner_id}"

    # -- conversation ----------------------------------------------------
    async def reply(
        self,
        scope: str,
        text: str,
        *,
        author_id: int | str | None = None,
        author_name: str | None = None,
        images: list[bytes] | None = None,
        label_speakers: bool = False,
    ) -> list[str]:
        """Compose and store a full reply in one go."""
        chunks = await self.compose(
            scope,
            text,
            author_id=author_id,
            author_name=author_name,
            images=images,
            label_speakers=label_speakers,
        )
        self.record_reply(scope, chunks)
        return chunks

    async def compose(
        self,
        scope: str,
        text: str,
        *,
        author_id: int | str | None = None,
        author_name: str | None = None,
        images: list[bytes] | None = None,
        label_speakers: bool = False,
        adjust: bool = False,
    ) -> list[str]:
        """Work out what to say, without storing her side yet.

        The caller decides what was actually delivered, which matters when he
        says something new mid-burst and the rest gets thrown away.
        """
        text = text.strip()
        images = [blob for blob in (images or []) if blob]
        if not text and not images:
            return []

        photo_urls: list[str] = []
        if images and self.vision_enabled:
            photo_urls = prepare_images(images)
            if not photo_urls:
                log.warning("could not decode the attached image(s)")

        store = self.store_for(scope)
        idle = store.idle_minutes(scope)
        stored = text
        if images:
            stored = f"{text} {PHOTO_MARKER}".strip() if text else PHOTO_MARKER
        store.add_message(
            scope, "user", stored, author_id=author_id, author_name=author_name
        )

        messages = self.build_messages(
            scope,
            idle_minutes=idle,
            attach_photo=bool(photo_urls),
            label_speakers=label_speakers,
            adjust=adjust,
        )
        if photo_urls:
            messages[-1] = {
                "role": "user",
                "content": to_content_parts(text, photo_urls),
            }
        raw = await self.client.chat(messages)
        chunks = split_messages(
            raw,
            max_chunks=self.settings.chat.max_chunks,
            max_chars=self.settings.chat.chunk_chars,
        )
        return chunks or [FALLBACKS[0]]

    def record_reply(self, scope: str, chunks: list[str]) -> None:
        """Store the texts she actually sent, then maybe consolidate."""
        store = self.store_for(scope)
        for chunk in chunks:
            store.add_message(scope, "assistant", chunk)
        if chunks:
            self.maybe_consolidate(scope)

    async def initiate(
        self, scope: str, *, follow_up: bool = False, worry_level: int = 1
    ) -> list[str]:
        """Start a conversation unprompted. Returns [] if the model has nothing."""
        messages = self.build_messages(
            scope, proactive=True, follow_up=follow_up, worry_level=worry_level
        )
        raw = await self.client.chat(
            messages,
            temperature=min(1.1, self.settings.model.temperature + 0.1),
        )
        chunks = split_messages(
            raw,
            max_chunks=min(2, self.settings.chat.max_chunks),
            max_chars=self.settings.chat.chunk_chars,
        )
        if not chunks:
            return []
        store = self.store_for(scope)
        for chunk in chunks:
            store.add_message(scope, "assistant", chunk)
        self.maybe_consolidate(scope)
        return chunks

    def build_messages(
        self,
        scope: str,
        *,
        idle_minutes: float | None = None,
        proactive: bool = False,
        attach_photo: bool = False,
        label_speakers: bool = False,
        follow_up: bool = False,
        worry_level: int = 1,
        adjust: bool = False,
    ) -> list[dict[str, Any]]:
        cfg = self.settings
        store = self.store_for(scope)
        history = self._trim(
            store.recent(scope, cfg.chat.history_messages),
            cfg.chat.history_token_budget,
        )
        history_ids = [int(m["id"]) for m in history]

        last_user = next(
            (m for m in reversed(history) if m["role"] == "user"), None
        )
        query = last_user["content"] if last_user else ""
        hits = store.search(
            query,
            cfg.memory.recall_hits,
            scope=scope,
            exclude_ids=history_ids,
        )

        system = build_system_prompt(
            cfg,
            facts=[
                fact["content"]
                for fact in store.facts(scope, cfg.memory.facts_limit)
            ],
            summaries=store.summaries(scope, cfg.memory.summaries_in_context)
            + self._recall_block(hits),
            idle_minutes=idle_minutes,
            proactive=proactive,
            timetable=self.timetable if self.is_owner(scope) else None,
            academic=self.academic if self.is_owner(scope) else None,
            photo=attach_photo,
            follow_up=follow_up,
            worry_level=worry_level,
            adjust=adjust,
            routine=self._routine(scope),
            keep_alive=self._should_keep_alive(history, proactive=proactive),
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        labelled = label_speakers
        for m in history:
            content = m["content"]
            if labelled and m["role"] == "user" and m.get("author_name"):
                content = f"{m['author_name']}: {content}"
            messages.append(
                {"role": "assistant" if m["role"] == "assistant" else "user",
                 "content": content}
            )
        if proactive:
            if follow_up:
                cue = (
                    FOLLOW_UP_WORRIED_CUE if worry_level >= 2 else FOLLOW_UP_CUE
                )
            else:
                cue = PROACTIVE_CUE
            messages.append({"role": "user", "content": cue})
        return messages

    def _routine(self, scope: str) -> str | None:
        """What her message history says about when he sleeps."""
        window = self.store_for(scope).quiet_window(scope)
        if window is None:
            return None
        start, end = window
        return (
            f"he is usually asleep between {_clock(start)} and {_clock(end)}, "
            "so quiet then is normal."
        )

    def _should_keep_alive(
        self, history: list[dict[str, Any]], *, proactive: bool
    ) -> bool:
        """Nudge her when the last few texts were pure reactions."""
        if proactive or not self.settings.chat.keep_conversation:
            return False
        her_last = [m for m in history if m["role"] == "assistant"][-3:]
        return bool(her_last) and not any("?" in m["content"] for m in her_last)

    @staticmethod
    def _recall_block(hits: list[dict[str, Any]]) -> list[str]:
        out: list[str] = []
        for hit in hits:
            stamp = str(hit.get("created_at") or "")[:10]
            who = "you" if hit["role"] == "assistant" else "him"
            snippet = " ".join(str(hit["content"]).split())[:220]
            out.append(f"{who} said on {stamp}: {snippet}")
        return out

    def _trim(
        self, history: list[dict[str, Any]], budget: int
    ) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        used = 0
        for message in reversed(history):
            cost = estimate_tokens(str(message["content"])) + 4
            if kept and used + cost > budget:
                break
            kept.append(message)
            used += cost
        kept.reverse()
        return kept[-self.settings.chat.history_messages :]

    # -- memory upkeep ---------------------------------------------------
    def maybe_consolidate(self, scope: str) -> None:
        if self._consolidating:
            return
        store = self.store_for(scope)
        pending = store.count_since(scope, store.last_summary_msg_id(scope))
        if pending < self.settings.memory.summarize_after:
            return
        self._consolidating = True
        asyncio.create_task(self._consolidate(scope))

    async def _consolidate(self, scope: str) -> None:
        try:
            await consolidate_mod.consolidate(
                self.store_for(scope),
                self.client,
                scope,
                batch=self.settings.memory.summarize_batch,
            )
        except Exception:
            log.exception("background consolidation failed")
        finally:
            self._consolidating = False

    # -- extras ----------------------------------------------------------
    def recall(
        self, query: str, limit: int = 8, *, scope: str | None = None
    ) -> list[dict[str, Any]]:
        if scope is None:
            return self.store_for("_shared").search(query, limit)
        return self.store_for(scope).search(query, limit, scope=scope)

    async def close(self) -> None:
        await self.server.stop()
        stores = list(self._stores.values())
        self._stores.clear()
        if self._injected_store is not None:
            stores.append(self._injected_store)
        for store in stores:
            try:
                store.close()
            except Exception:
                log.debug("could not close a memory file", exc_info=True)
