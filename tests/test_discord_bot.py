from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import discord

from nora import config
from nora.discord_bot import NoraBot
from nora.memory import MemoryStore
from nora.proactive import Decision


class FakeTyping:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class FakeChannel:
    def __init__(self, cid: int):
        self.id = cid
        self.guild = None
        self.sent: list[str] = []

    def typing(self) -> FakeTyping:
        return FakeTyping()

    async def send(self, text: str) -> None:
        self.sent.append(text)


class FakeAuthor:
    def __init__(self, uid: int = 1, name: str = "mark"):
        self.id = uid
        self.display_name = name
        self.bot = False


class StubNora:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.calls: list[tuple[str, str, bool]] = []
        self.stored: list[str] = []
        self.answers = ["part one", "part two"]
        self.on_compose = None

    async def compose(self, scope, text, **kwargs):
        self.calls.append((scope, text, bool(kwargs.get("adjust", False))))
        if self.on_compose is not None:
            await self.on_compose(scope, text)
        return list(self.answers)

    def record_reply(self, scope, chunks):
        for chunk in chunks:
            self.stored.append(chunk)
            self.store.add_message(scope, "assistant", chunk)

    def store_for(self, scope):
        return self.store

    def recall(self, *args, **kwargs):
        return []

    async def close(self):
        self.store.close()


def _settings(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "bot.db")
    settings.chat.debounce = 0.05
    settings.chat.reply_delay.enabled = False  # tests must not wait minutes
    settings.chat.min_delay = 0.01
    settings.chat.max_delay = 0.02
    settings.chat.typing_cps = 1000
    return settings


def test_batches_split_messages_into_one_reply(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        store = MemoryStore(settings.path(settings.memory.db_path))
        stub = StubNora(store)
        bot = NoraBot(settings, stub)
        channel = FakeChannel(42)
        author = FakeAuthor()

        bot._queue(SimpleNamespace(channel=channel, author=author), "hey", [])
        bot._queue(SimpleNamespace(channel=channel, author=author), "you around", [])
        await asyncio.sleep(0.3)

        assert [(scope, text) for scope, text, _ in stub.calls] == [
            ("u:1", "hey\nyou around")
        ]
        assert channel.sent == ["part one", "part two"]
        await bot.close()

    asyncio.run(scenario())


def test_flush_consults_the_reply_delay(tmp_path, monkeypatch):
    seen: list[float] = []

    def fake_wait(cfg, *, burst_age):
        seen.append(burst_age)
        return 0.0

    monkeypatch.setattr("nora.discord_bot.pacing.compute_wait", fake_wait)

    async def scenario():
        settings = _settings(tmp_path)
        stub = StubNora(MemoryStore(tmp_path / "b4.db"))
        bot = NoraBot(settings, stub)
        channel = FakeChannel(77)

        bot._queue(SimpleNamespace(channel=channel, author=FakeAuthor()), "hi", [])
        assert 77 in bot._pending_since
        await asyncio.sleep(0.3)

        assert seen and seen[0] >= 0.0
        assert stub.calls and stub.calls[0][0] == "u:1"
        assert 77 not in bot._pending_since
        await bot.close()

    asyncio.run(scenario())


def test_adapts_when_he_types_while_she_composes(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        stub = StubNora(MemoryStore(tmp_path / "b10.db"))
        bot = NoraBot(settings, stub)
        channel = FakeChannel(31)
        author = FakeAuthor()

        state = {"first": True}

        async def on_compose(scope, text):
            if state["first"]:
                state["first"] = False
                bot._queue(
                    SimpleNamespace(channel=channel, author=author),
                    "wait i forgot to say, i got the job",
                    [],
                )

        stub.on_compose = on_compose
        bot._queue(
            SimpleNamespace(channel=channel, author=author), "i passed the exam", []
        )
        await asyncio.sleep(0.6)

        assert [text for _, text, _ in stub.calls] == [
            "i passed the exam",
            "wait i forgot to say, i got the job",
        ]
        assert stub.calls[1][2] is True  # retried with adjust=True
        # only the adjusted burst went out, and only that is remembered
        assert channel.sent == ["part one", "part two"]
        assert stub.stored == ["part one", "part two"]
        await bot.close()

    asyncio.run(scenario())


def test_drops_the_rest_of_the_burst_if_he_speaks(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        stub = StubNora(MemoryStore(tmp_path / "b11.db"))
        bot = NoraBot(settings, stub)
        channel = FakeChannel(32)
        author = FakeAuthor()

        state = {"queued": False}
        original_send = channel.send

        async def send(text):
            await original_send(text)
            if len(channel.sent) == 1 and not state["queued"]:
                state["queued"] = True
                bot._queue(
                    SimpleNamespace(channel=channel, author=author),
                    "sorry, phone died",
                    [],
                )

        channel.send = send
        bot._queue(SimpleNamespace(channel=channel, author=author), "hey", [])
        await asyncio.sleep(0.9)

        # first burst cut after one text, then she answered the new message
        assert channel.sent == ["part one", "part one", "part two"]
        assert [text for _, text, _ in stub.calls] == ["hey", "sorry, phone died"]
        assert stub.stored == ["part one", "part one", "part two"]
        await bot.close()

    asyncio.run(scenario())


def test_presence_reflects_availability(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "b6.db")))
        seen = []

        async def fake_change_presence(**kwargs):
            seen.append(kwargs)

        bot.change_presence = fake_change_presence
        await bot._apply_presence()

        assert len(seen) == 1
        assert seen[0]["status"] in (
            discord.Status.online,
            discord.Status.idle,
            discord.Status.dnd,
        )
        assert seen[0]["activity"] is None or isinstance(
            seen[0]["activity"], discord.CustomActivity
        )
        await bot.close()

    asyncio.run(scenario())


def test_presence_shows_idle_while_she_sleeps(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        hour = datetime.now().hour
        settings.presence.sleep_hours = [hour, (hour + 2) % 24]
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "b7.db")))
        seen = []

        async def fake_change_presence(**kwargs):
            seen.append(kwargs)

        bot.change_presence = fake_change_presence
        await bot._apply_presence()

        assert seen[0]["status"] is discord.Status.idle
        await bot.close()

    asyncio.run(scenario())


def test_address_detection(tmp_path):
    class BotWithUser(NoraBot):
        user = FakeAuthor(99, "nora")

    async def scenario():
        bot = BotWithUser(
            _settings(tmp_path), StubNora(MemoryStore(tmp_path / "b.db"))
        )

        dm = SimpleNamespace(guild=None, mentions=[], reference=None)
        assert bot._is_addressed(dm) is True

        plain = SimpleNamespace(guild=object(), mentions=[], reference=None)
        assert bot._is_addressed(plain) is False

        mentioned = SimpleNamespace(
            guild=object(), mentions=[BotWithUser.user], reference=None
        )
        assert bot._is_addressed(mentioned) is True

        await bot.close()

    asyncio.run(scenario())


def test_allowlist_matches_thread_parent(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        settings.discord.channels = [7]
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "b.db")))

        assert bot._in_allowlist(SimpleNamespace(id=7)) is True
        assert bot._in_allowlist(SimpleNamespace(id=8, parent_id=7)) is True
        assert bot._in_allowlist(SimpleNamespace(id=8, parent_id=None)) is False

        await bot.close()

    asyncio.run(scenario())


def test_next_attempt_jumps_past_blocked_windows(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "b2.db")))
        now = datetime(2026, 11, 1, 1, 0)
        cfg = settings.proactive

        quiet = bot._next_attempt(now, Decision(False, "quiet hours"), cfg)
        assert quiet >= 6.5 * 3600

        daily = bot._next_attempt(now, Decision(False, "daily limit reached"), cfg)
        assert daily >= 22 * 3600

        normal = bot._next_attempt(now, Decision(False, "you talked recently"), cfg)
        assert cfg.min_gap_minutes * 60 <= normal <= cfg.max_gap_minutes * 60

        await bot.close()

    asyncio.run(scenario())


def test_each_person_gets_their_own_scope(tmp_path):
    settings = _settings(tmp_path)
    bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "b8.db")))

    first = SimpleNamespace(author=FakeAuthor(1), channel=FakeChannel(10), guild=None)
    second = SimpleNamespace(author=FakeAuthor(2), channel=FakeChannel(10), guild=None)

    assert bot._scope(first) == "u:1"
    assert bot._scope(second) == "u:2"
    bot.nora.store.close()


def test_allow_users_can_block_strangers(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        settings.discord.allow_users = [1]
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "b9.db")))

        assert bot._user_allowed(1) is True
        assert bot._user_allowed(2) is False

        settings.discord.allow_users = []
        assert bot._user_allowed(2) is True
        await bot.close()

    asyncio.run(scenario())


def test_unanswered_streak_counts_her_texts(tmp_path):
    async def scenario():
        store = MemoryStore(tmp_path / "b3.db")
        bot = NoraBot(_settings(tmp_path), StubNora(store))

        store.add_message("dm:1", "user", "hi")
        assert bot._unanswered_streak("dm:1") == 0

        store.add_message("dm:1", "assistant", "ugh")
        store.add_message("dm:1", "assistant", "what")
        assert bot._unanswered_streak("dm:1") == 2

        store.add_message("dm:1", "user", "hey")
        assert bot._unanswered_streak("dm:1") == 0

        await bot.close()

    asyncio.run(scenario())
