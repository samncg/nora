from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord

from nora import config, greetings, reminders
from nora.discord_bot import NoraBot
from nora.memory import MemoryStore
from nora.proactive import Decision
from nora.reminders import Reminder


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
        self.typing_calls = 0

    def typing(self) -> FakeTyping:
        self.typing_calls += 1
        return FakeTyping()

    async def send(self, text: str) -> None:
        self.sent.append(text)


class FakeAuthor:
    def __init__(self, uid: int = 1, name: str = "mark"):
        self.id = uid
        self.display_name = name
        self.bot = False


class FakeAttachment:
    def __init__(
        self,
        data: bytes = b"audio",
        *,
        content_type: str = "audio/ogg",
        filename: str = "voice-message.ogg",
    ):
        self.size = len(data)
        self.content_type = content_type
        self.filename = filename
        self._data = data

    def is_voice_message(self) -> bool:
        return True

    async def read(self) -> bytes:
        return self._data


class FakeTranscriber:
    def __init__(self, text: str):
        self.text = text

    def transcribe_many(self, clips) -> str:
        return self.text


class FakeSpeaker:
    def __init__(self, speech=None):
        self.speech = speech
        self.texts: list[str] = []

    def speak_chunks(self, chunks):
        self.texts.append(" ".join(chunks))
        return self.speech

    def speak(self, text: str):
        return self.speak_chunks([text])


def _voice_message(channel, author, attachment):
    return SimpleNamespace(
        channel=channel,
        author=author,
        guild=None,
        content="",
        attachments=[attachment],
        stickers=[],
        mentions=[],
        reference=None,
        flags=SimpleNamespace(voice=True),
    )


class FakeSent:
    def __init__(self, content: str):
        self.content = content
        self.edits: list[str] = []

    async def edit(self, content: str) -> None:
        self.edits.append(content)
        self.content = content


class FakeReactable:
    def __init__(self):
        self.reactions: list[str] = []

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)


class StubNora:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.owner_id: int | None = None
        self.calls: list[tuple[str, str, bool]] = []
        self.stored: list[str] = []
        self.answers = ["part one", "part two"]
        self.on_compose = None

    def is_owner(self, scope: str) -> bool:
        return self.owner_id is not None and scope == f"u:{self.owner_id}"

    async def compose(self, scope, text, **kwargs):
        self.calls.append((scope, text, bool(kwargs.get("adjust", False))))
        if self.on_compose is not None:
            await self.on_compose(scope, text)
        return list(self.answers)

    async def initiate(self, scope, **kwargs):
        self.calls.append((scope, "initiate", bool(kwargs.get("occasion"))))
        return list(self.answers) if self.answers else []

    def record_reply(self, scope, chunks):
        for chunk in chunks:
            self.stored.append(chunk)
            self.store.add_message(scope, "assistant", chunk)

    def store_for(self, scope):
        return self.store

    def recall(self, *args, **kwargs):
        return []

    def mood_pace(self, scope):
        return 1.0

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
    settings.chat.bubble_gap_min = 0.0
    settings.chat.bubble_gap_max = 0.0
    settings.chat.typing_tease_chance = 0.0
    settings.chat.think_pause_chance = 0.0
    settings.chat.self_correct_chance = 0.0
    settings.chat.self_correct_delay_min = 0.0
    settings.chat.self_correct_delay_max = 0.0
    settings.chat.rethink_delay_min = 0.01
    settings.chat.rethink_delay_max = 0.02
    settings.chat.return_delay_min = 0.0
    settings.chat.return_delay_max = 0.0
    settings.chat.waking_minutes = 0.0
    settings.chat.wake_delay_min = 0.0
    settings.chat.wake_delay_max = 0.0
    settings.tts.enabled = False  # never synthesize or upload in unit tests
    settings.reactions.enabled = False  # reactions are tested directly
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


def test_voice_message_is_transcribed_before_replying(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        stub = StubNora(MemoryStore(tmp_path / "voice.db"))
        bot = NoraBot(settings, stub)
        bot.transcriber = FakeTranscriber("hey are you free tomorrow")
        channel = FakeChannel(55)
        author = FakeAuthor()

        await bot.on_message(_voice_message(channel, author, FakeAttachment()))
        await asyncio.sleep(0.3)

        assert [text for _, text, _ in stub.calls] == [
            "hey are you free tomorrow"
        ]
        assert channel.sent == ["part one", "part two"]
        await bot.close()

    asyncio.run(scenario())


def test_silent_voice_message_gets_a_fallback(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        stub = StubNora(MemoryStore(tmp_path / "voice2.db"))
        bot = NoraBot(settings, stub)
        bot.transcriber = FakeTranscriber("")
        channel = FakeChannel(56)
        author = FakeAuthor()

        await bot.on_message(_voice_message(channel, author, FakeAttachment()))

        assert channel.sent == ["i couldnt hear that, say it again?"]
        assert stub.stored == ["i couldnt hear that, say it again?"]
        assert stub.store.recent("u:1", 5)[0]["content"] == "[sent a voice message]"
        await bot.close()

    asyncio.run(scenario())


def test_voice_note_gets_a_voice_reply(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        settings.tts.enabled = True
        stub = StubNora(MemoryStore(tmp_path / "voice3.db"))
        bot = NoraBot(settings, stub)
        bot.transcriber = FakeTranscriber("hey are you free tomorrow")
        speech = SimpleNamespace(duration=1.2, opus=b"ogg", waveform=b"\x00" * 256)
        speaker = FakeSpeaker(speech)
        bot.speaker = speaker
        sent = []

        async def send_voice(channel, clip, reply_to=None):
            sent.append(clip)

        bot._send_voice_clip = send_voice
        channel = FakeChannel(57)
        author = FakeAuthor()

        await bot.on_message(_voice_message(channel, author, FakeAttachment()))
        await asyncio.sleep(0.3)

        assert speaker.texts == ["part one part two"]
        assert sent == [speech]
        assert channel.sent == []  # no duplicate text reply
        assert stub.stored == ["part one", "part two"]
        await bot.close()

    asyncio.run(scenario())


def test_voice_reply_falls_back_to_text(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        settings.tts.enabled = True
        stub = StubNora(MemoryStore(tmp_path / "voice4.db"))
        bot = NoraBot(settings, stub)
        bot.transcriber = FakeTranscriber("hey")
        bot.speaker = FakeSpeaker(None)  # synthesis failed
        channel = FakeChannel(58)
        author = FakeAuthor()

        await bot.on_message(_voice_message(channel, author, FakeAttachment()))
        await asyncio.sleep(0.3)

        assert channel.sent == ["part one", "part two"]
        await bot.close()

    asyncio.run(scenario())


def test_voice_message_upload_uses_discord_voice_fields(tmp_path, monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr("nora.discord_bot.httpx.AsyncClient", FakeClient)

    async def scenario():
        settings = _settings(tmp_path)
        settings.discord.token = "test-token"
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "voice5.db")))
        speech = SimpleNamespace(
            duration=1.5, opus=b"OggS-data", waveform=b"\x01" * 256
        )
        await bot._send_voice_clip(SimpleNamespace(id=123), speech)
        await bot.close()

    asyncio.run(scenario())

    import json

    payload = json.loads(captured["data"]["payload_json"])
    assert payload["flags"] == 8192
    assert payload["attachments"][0]["filename"] == "voice-message.ogg"
    assert payload["attachments"][0]["duration_secs"] == 1.5
    assert len(payload["attachments"][0]["waveform"]) > 0
    assert captured["files"]["files[0]"][0] == "voice-message.ogg"
    assert captured["url"].endswith("/channels/123/messages")


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


def test_wake_delay_once_per_day(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.chat.waking_minutes = 45
    settings.chat.wake_delay_min = 2
    settings.chat.wake_delay_max = 3
    monkeypatch.setattr(
        "nora.discord_bot.presence.just_woke", lambda *args, **kwargs: True
    )
    bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "wake.db")))

    first = bot._wake_delay("u:1")
    assert 2.0 <= first <= 3.0
    assert bot._wake_delay("u:1") == 0.0
    assert 2.0 <= bot._wake_delay("u:2") <= 3.0
    asyncio.run(bot.close())


def test_rethink_delay_uses_configured_range(tmp_path):
    settings = _settings(tmp_path)
    settings.chat.rethink_delay_min = 2.0
    settings.chat.rethink_delay_max = 3.0
    bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "rethink.db")))
    assert 2.0 <= bot._rethink_delay() <= 3.0
    asyncio.run(bot.close())


def test_return_delay_only_after_a_long_gap(tmp_path):
    settings = _settings(tmp_path)
    settings.chat.long_gap_minutes = 180
    settings.chat.return_delay_min = 5
    settings.chat.return_delay_max = 6
    bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "return.db")))
    assert bot._return_delay(None) == 0.0
    assert bot._return_delay(30) == 0.0
    assert 5.0 <= bot._return_delay(200) <= 6.0
    asyncio.run(bot.close())


def test_bubble_gap_uses_configured_range(tmp_path):
    settings = _settings(tmp_path)
    settings.chat.bubble_gap_min = 0.4
    settings.chat.bubble_gap_max = 0.9
    bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "bubble.db")))
    assert 0.4 <= bot._bubble_gap() <= 0.9
    asyncio.run(bot.close())


def test_think_pause_uses_configured_range(tmp_path):
    settings = _settings(tmp_path)
    settings.chat.think_pause_chance = 1.0
    settings.chat.think_pause_min = 2.0
    settings.chat.think_pause_max = 4.0
    bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "think.db")))
    assert 2.0 <= bot._think_pause() <= 4.0
    asyncio.run(bot.close())


def test_think_pause_can_be_disabled(tmp_path):
    settings = _settings(tmp_path)
    settings.chat.think_pause_chance = 0.0
    bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "think_off.db")))
    assert bot._think_pause() == 0.0
    asyncio.run(bot.close())


def test_think_pause_stops_typing_then_resumes(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        settings.chat.think_pause_chance = 1.0
        settings.chat.think_pause_min = 0.01
        settings.chat.think_pause_max = 0.02
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "think2.db")))
        channel = FakeChannel(62)

        await bot._send_chunk(channel, "hello there")

        assert channel.sent == ["hello there"]
        # typed, dropped the indicator while thinking, then typed again
        assert channel.typing_calls == 2
        await bot.close()

    asyncio.run(scenario())


def test_typo_variant_returns_the_original_as_correction(tmp_path):
    settings = _settings(tmp_path)
    settings.chat.self_correct_chance = 1.0
    bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "typo.db")))
    typo, correction = bot._typo_variant("hello friend")
    assert correction == "hello friend"
    assert typo != correction
    asyncio.run(bot.close())


def test_send_bubble_sends_typo_then_edits(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        settings.chat.self_correct_chance = 1.0
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "typo2.db")))
        channel = FakeChannel(61)
        sent_messages: list[FakeSent] = []

        async def send(text):
            message = FakeSent(text)
            sent_messages.append(message)
            channel.sent.append(text)
            return message

        channel.send = send
        _, final = await bot._send_bubble(channel, "hello friend")

        assert final == "hello friend"
        assert channel.sent[0] != "hello friend"
        assert sent_messages[0].edits == ["hello friend"]
        await bot.close()

    asyncio.run(scenario())


def test_pick_reaction_matches_tone():
    assert NoraBot._pick_reaction("i miss you") == "❤️"
    assert NoraBot._pick_reaction("i got the job") == "🔥"
    assert NoraBot._pick_reaction("rough day") == "😭"
    assert NoraBot._pick_reaction("lmao no way") == "💀"
    # no clear cue means no reaction, never a random one
    assert NoraBot._pick_reaction("neutral words") is None
    assert NoraBot._pick_reaction("my deadline is thursday") is None
    assert NoraBot._pick_reaction("") is None


def test_farewell_cues():
    assert NoraBot._is_farewell("ttyl")
    assert NoraBot._is_farewell("goodbye!")
    assert NoraBot._is_farewell("gotta go")
    assert NoraBot._is_farewell("see ya")
    assert NoraBot._is_farewell("goodnight")
    assert not NoraBot._is_farewell("good movie")
    assert not NoraBot._is_farewell("what time is it")


def test_farewell_always_gets_a_heart(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        settings.reactions.enabled = True
        settings.reactions.chance = 0.0  # normal reactions never roll
        settings.reactions.cooldown_minutes = 60
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "farewell.db")))
        message = FakeReactable()

        await bot._maybe_react(message, "u:1", "ttyl")
        assert message.reactions == ["❤️"]

        # the cooldown must not swallow a second goodbye
        await bot._maybe_react(message, "u:1", "goodnight!")
        assert message.reactions == ["❤️", "❤️"]

        # a normal cued message still obeys the chance roll
        await bot._maybe_react(message, "u:1", "lmao no way")
        assert message.reactions == ["❤️", "❤️"]
        await bot.close()

    asyncio.run(scenario())


def test_farewell_reaction_is_scheduled(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        settings.reactions.enabled = True
        settings.reactions.chance = 0.0
        settings.reactions.delay_min = 0.05
        settings.reactions.delay_max = 0.05
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "farewell2.db")))
        message = FakeReactable()

        bot._schedule_reaction(message, "u:1", "gotta go")
        assert message.reactions == []
        await asyncio.sleep(0.2)
        assert message.reactions == ["❤️"]
        await bot.close()

    asyncio.run(scenario())


def test_reaction_note_is_compact():
    assert (
        NoraBot._reaction_note("😭", "  you   ok? ")
        == '[reacted 😭 to your message: "you ok?"]'
    )


def test_maybe_react_skips_neutral_text(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        settings.reactions.enabled = True
        settings.reactions.chance = 1.0
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "react_neutral.db")))
        message = FakeReactable()

        await bot._maybe_react(message, "u:1", "what time is it")
        assert message.reactions == []
        await bot.close()

    asyncio.run(scenario())


def test_reaction_is_delayed(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        settings.reactions.enabled = True
        settings.reactions.chance = 1.0
        settings.reactions.delay_min = 0.05
        settings.reactions.delay_max = 0.05
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "react_delay.db")))
        message = FakeReactable()

        bot._schedule_reaction(message, "u:1", "i miss you")
        # it does not land the instant the message arrives
        assert message.reactions == []

        await asyncio.sleep(0.2)
        assert message.reactions == ["❤️"]
        await bot.close()

    asyncio.run(scenario())


def test_maybe_react_respects_cooldown(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        settings.reactions.enabled = True
        settings.reactions.chance = 1.0
        settings.reactions.cooldown_minutes = 20
        bot = NoraBot(settings, StubNora(MemoryStore(tmp_path / "react.db")))
        message = FakeReactable()

        await bot._maybe_react(message, "u:1", "i miss you")
        assert message.reactions == ["❤️"]

        await bot._maybe_react(message, "u:1", "i got the job")
        assert message.reactions == ["❤️"]  # still cooling down

        await bot._maybe_react(message, "u:2", "i got the job")
        assert message.reactions == ["❤️", "🔥"]
        await bot.close()

    asyncio.run(scenario())


def test_user_reaction_is_remembered(tmp_path):
    class BotWithUser(NoraBot):
        user = SimpleNamespace(id=99)

    async def scenario():
        settings = _settings(tmp_path)
        settings.reactions.enabled = True
        settings.reactions.remember_user_reactions = True
        store = MemoryStore(tmp_path / "react2.db")
        bot = BotWithUser(settings, StubNora(store))
        message = SimpleNamespace(
            content="you ok?",
            author=SimpleNamespace(id=99),
        )

        async def fetch_message(message_id):
            return message

        channel = SimpleNamespace(fetch_message=fetch_message)
        bot.get_channel = lambda cid: channel
        payload = SimpleNamespace(
            user_id=1,
            channel_id=5,
            message_id=7,
            emoji="😭",
            member=None,
        )

        await bot.on_raw_reaction_add(payload)

        recent = store.recent("u:1", 1)
        assert recent[0]["content"] == '[reacted 😭 to your message: "you ok?"]'
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


# -- reminders, greetings, and his presence ---------------------------------
class FakeMember:
    def __init__(self, uid: int, status: str = "online", activities=()):
        self.id = uid
        self.status = SimpleNamespace(name=status)
        self.activities = list(activities)


class FakeActivity:
    def __init__(self, name: str, kind: str = "playing"):
        self.name = name
        self.type = SimpleNamespace(name=kind)


def _bot(tmp_path, **greeting_overrides):
    settings = _settings(tmp_path)
    settings.proactive.quiet_hours = [0, 0]  # never quiet during tests
    for key, value in greeting_overrides.items():
        setattr(settings.greetings, key, value)
    store = MemoryStore(settings.path(settings.memory.db_path))
    stub = StubNora(store)
    stub.answers = []  # no model output, so the template fallback is used
    bot = NoraBot(settings, stub)
    channel = FakeChannel(7)
    bot._direct_channel = channel
    bot._proactive_uid = 1
    return bot, stub, store, channel


def test_events_in_a_message_are_remembered(tmp_path):
    async def scenario():
        bot, stub, store, _ = _bot(tmp_path)
        stub.owner_id = 1
        bot._remember_events("u:1", "i have an exam tmr at 2pm")

        saved = reminders.load(store)
        assert len(saved) == 1
        assert saved[0].title == "exam"
        await bot.close()

    asyncio.run(scenario())


def test_other_people_do_not_get_reminders(tmp_path):
    async def scenario():
        bot, stub, store, _ = _bot(tmp_path)
        stub.owner_id = 1
        bot._remember_events("u:2", "i have an exam tmr at 2pm")
        assert reminders.load(store) == []
        await bot.close()

    asyncio.run(scenario())


def test_ordinary_chatter_remembers_nothing(tmp_path):
    async def scenario():
        bot, stub, store, _ = _bot(tmp_path)
        stub.owner_id = 1
        bot._remember_events("u:1", "you free tomorrow?")
        assert reminders.load(store) == []
        await bot.close()

    asyncio.run(scenario())


def test_a_due_reminder_is_texted_back(tmp_path):
    async def scenario():
        bot, stub, store, channel = _bot(tmp_path)
        now = datetime.now().replace(microsecond=0)
        reminders.save(
            store,
            [
                Reminder(
                    title="exam",
                    when=(now + timedelta(minutes=10)).isoformat(timespec="minutes"),
                    lead_minutes=30,
                    created_at=now.isoformat(timespec="minutes"),
                )
            ],
        )

        assert await bot._check_reminders(now) is True
        assert len(channel.sent) == 1
        assert "your exam is" in channel.sent[0]
        assert "dont forget" in channel.sent[0]
        # telling her about an occasion passes the occasion to the model
        assert stub.calls[-1] == ("u:1", "initiate", True)

        # it must not fire twice
        assert await bot._check_reminders(now + timedelta(minutes=1)) is False
        assert len(channel.sent) == 1

        # and it is marked as sent in her memory
        assert reminders.due(reminders.load(store), now) == []
        await bot.close()

    asyncio.run(scenario())


def test_a_reminder_waits_for_its_lead_window(tmp_path):
    async def scenario():
        bot, _, store, channel = _bot(tmp_path)
        now = datetime.now().replace(microsecond=0)
        reminders.save(
            store,
            [
                Reminder(
                    title="exam",
                    when=(now + timedelta(minutes=90)).isoformat(timespec="minutes"),
                    lead_minutes=30,
                    created_at=now.isoformat(timespec="minutes"),
                )
            ],
        )

        assert await bot._check_reminders(now) is False
        assert channel.sent == []
        await bot.close()

    asyncio.run(scenario())


def test_greeting_goes_out_once_a_day(tmp_path):
    async def scenario():
        bot, stub, _, channel = _bot(tmp_path)
        morning = datetime(2026, 11, 4, 9, 0)

        assert await bot._maybe_greet(morning) is True
        assert len(channel.sent) == 1
        assert channel.sent[0].startswith("morning")
        assert stub.calls[-1] == ("u:1", "initiate", True)

        assert await bot._maybe_greet(morning + timedelta(minutes=40)) is False
        assert len(channel.sent) == 1

        # the next day she may say it again
        assert await bot._maybe_greet(morning + timedelta(days=1)) is True
        assert len(channel.sent) == 2
        await bot.close()

    asyncio.run(scenario())


def test_no_greeting_in_the_afternoon(tmp_path):
    async def scenario():
        bot, _, _, channel = _bot(tmp_path)
        assert await bot._maybe_greet(datetime(2026, 11, 4, 15, 0)) is False
        assert channel.sent == []
        await bot.close()

    asyncio.run(scenario())


def test_no_greeting_right_after_they_talked(tmp_path):
    async def scenario():
        bot, _, store, channel = _bot(tmp_path)
        store.add_message("u:1", "user", "hey")

        assert await bot._maybe_greet(datetime(2026, 11, 4, 9, 0)) is False
        assert channel.sent == []
        await bot.close()

    asyncio.run(scenario())


def test_activity_is_only_cached_for_the_owner(tmp_path):
    async def scenario():
        bot, stub, _, _ = _bot(tmp_path)
        stub.owner_id = 5

        bot._remember_activity(
            FakeMember(6, "dnd", [FakeActivity("Homework")])
        )
        assert bot._owner_activity is None

        bot._remember_activity(
            FakeMember(5, "online", [FakeActivity("Minecraft")])
        )
        assert bot._owner_activity == "he is online and playing Minecraft"
        await bot.close()

    asyncio.run(scenario())


def test_his_activity_only_reaches_his_own_prompt(tmp_path):
    async def scenario():
        bot, stub, _, _ = _bot(tmp_path)
        stub.owner_id = 1
        bot._owner_activity = "he is online and playing Minecraft"

        assert bot._owner_doing("u:1") == "he is online and playing Minecraft"
        assert bot._owner_doing("u:2") is None

        bot._track_owner = False
        assert bot._owner_doing("u:1") is None
        await bot.close()

    asyncio.run(scenario())


def test_presence_intents_follow_the_config(tmp_path):
    settings = _settings(tmp_path)
    settings.presence.track_owner = True
    assert NoraBot(settings, StubNora(MemoryStore(settings.path(settings.memory.db_path)))).intents.presences

    settings2 = _settings(tmp_path)
    settings2.presence.track_owner = False
    bot = NoraBot(
        settings2, StubNora(MemoryStore(settings2.path(settings2.memory.db_path)))
    )
    assert not bot.intents.presences
    assert not bot.intents.members


def test_privileged_off_means_no_presence_tracking(tmp_path):
    settings = _settings(tmp_path)
    settings.presence.track_owner = True
    bot = NoraBot(
        settings,
        StubNora(MemoryStore(settings.path(settings.memory.db_path))),
        privileged=False,
    )
    assert not bot.intents.presences
    assert not bot.intents.members
    assert not bot._track_owner


# -- learning his sleep schedule and the random reminder lead ---------------
def test_saying_goodnight_teaches_her_his_bedtime(tmp_path):
    async def scenario():
        bot, stub, store, _ = _bot(tmp_path)
        stub.owner_id = 1
        for day in (1, 2, 3):
            bot._note_greeting("u:1", "ok going to bed", datetime(2026, 11, day, 1, 10))

        profile = greetings.load_profile(store)
        assert len(profile[greetings.NIGHT]) == 3
        start, _ = greetings.window_for(greetings.NIGHT, bot.settings.greetings, profile)
        assert greetings.clock(start) == "00:40"
        await bot.close()

    asyncio.run(scenario())


def test_a_learned_window_moves_the_greeting(tmp_path):
    async def scenario():
        bot, stub, store, channel = _bot(tmp_path)
        stub.owner_id = 1
        # he greets her at 07:40, so she starts aiming at 07:25
        for day in (1, 2, 3):
            greetings.note_user_greeting(store, "good morning", datetime(2026, 11, day, 7, 40))

        # 07:30 is before the configured 08:00 window, but inside the learned one
        assert await bot._maybe_greet(datetime(2026, 11, 4, 7, 30)) is True
        assert len(channel.sent) == 1
        await bot.close()

    asyncio.run(scenario())


def test_other_people_do_not_teach_her_his_schedule(tmp_path):
    async def scenario():
        bot, stub, store, _ = _bot(tmp_path)
        stub.owner_id = 1
        bot._note_greeting("u:2", "goodnight", datetime(2026, 11, 4, 2, 0))
        assert greetings.load_profile(store)[greetings.NIGHT] == []
        await bot.close()

    asyncio.run(scenario())


def test_reminders_pick_a_human_lead(tmp_path):
    async def scenario():
        bot, stub, store, _ = _bot(tmp_path)
        stub.owner_id = 1
        stub.answers = []
        bot._remember_events("u:1", "i have an exam tmr at 2pm")

        saved = reminders.load(store)
        assert len(saved) == 1
        assert saved[0].notify_at is not None
        assert 10 <= saved[0].lead_minutes <= 50
        await bot.close()

    asyncio.run(scenario())


def test_the_history_seed_moves_the_greeting(tmp_path):
    async def scenario():
        bot, stub, store, channel = _bot(tmp_path)
        stub.owner_id = 1
        bot._greeting_seed = {"morning": 6.0, "night": 22.5}

        # 06:05 is nowhere near the configured 08:00 window
        assert await bot._maybe_greet(datetime(2026, 11, 4, 6, 5)) is True
        assert len(channel.sent) == 1
        await bot.close()

    asyncio.run(scenario())


def test_a_goodnight_inside_quiet_hours_still_goes_out(tmp_path):
    async def scenario():
        bot, stub, store, channel = _bot(tmp_path)
        stub.owner_id = 1
        bot.settings.proactive.quiet_hours = [0, 8]
        # his real bedtime, learned from his history
        bot._greeting_seed = {"morning": 11.0, "night": 2.5}

        assert await bot._maybe_greet(datetime(2026, 11, 5, 2, 30)) is True
        assert len(channel.sent) == 1
        await bot.close()

    asyncio.run(scenario())


# -- quoting the message she answers ----------------------------------------
class FakeReplyMessage:
    """A message with discord's reply button, recording what she quoted."""

    def __init__(self, channel, author_id=1, name="mark", mid=900):
        self.channel = channel
        self.author = FakeAuthor(author_id, name)
        self.id = mid
        self.replied: list[str] = []
        self.kwargs: list[dict] = []
        self.fail = False

    async def reply(self, content=None, **kwargs):
        if self.fail:
            raise RuntimeError("the message was deleted")
        self.replied.append(content)
        self.kwargs.append(kwargs)
        return FakeSent(content)


def _reply_bot(tmp_path, **chat_overrides):
    settings = _settings(tmp_path)
    for key, value in chat_overrides.items():
        setattr(settings.chat, key, value)
    store = MemoryStore(settings.path(settings.memory.db_path))
    bot = NoraBot(settings, StubNora(store))
    return bot


def test_first_bubble_is_a_discord_reply(tmp_path):
    async def scenario():
        bot = _reply_bot(tmp_path)
        channel = FakeChannel(42)
        message = FakeReplyMessage(channel)

        bot._queue(message, "hey", [])
        await asyncio.sleep(0.3)

        # the opening bubble goes out as a reply, the second as a plain send
        assert message.replied == ["part one"]
        assert channel.sent == ["part two"]
        # and it does not ping him
        assert message.kwargs[0]["mention_author"] is False
        await bot.close()

    asyncio.run(scenario())


def test_the_reply_points_at_the_last_message_of_the_burst(tmp_path):
    async def scenario():
        bot = _reply_bot(tmp_path)
        channel = FakeChannel(42)
        first = FakeReplyMessage(channel, mid=900)
        second = FakeReplyMessage(channel, mid=901)

        bot._queue(first, "hey", [])
        bot._queue(second, "you around", [])
        await asyncio.sleep(0.3)

        assert first.replied == []
        assert second.replied == ["part one"]
        await bot.close()

    asyncio.run(scenario())


def test_a_deleted_message_falls_back_to_a_plain_send(tmp_path):
    async def scenario():
        bot = _reply_bot(tmp_path)
        channel = FakeChannel(42)
        message = FakeReplyMessage(channel)
        message.fail = True

        bot._queue(message, "hey", [])
        await asyncio.sleep(0.3)

        assert channel.sent == ["part one", "part two"]
        assert message.replied == []
        await bot.close()

    asyncio.run(scenario())


def test_replies_can_be_turned_off(tmp_path):
    async def scenario():
        bot = _reply_bot(tmp_path, reply_to_messages=False)
        channel = FakeChannel(42)
        message = FakeReplyMessage(channel)

        bot._queue(message, "hey", [])
        await asyncio.sleep(0.3)

        assert message.replied == []
        assert channel.sent == ["part one", "part two"]
        await bot.close()

    asyncio.run(scenario())


def test_dm_replies_can_be_skipped_while_server_replies_stay(tmp_path):
    async def scenario():
        bot = _reply_bot(tmp_path, reply_in_dms=False)

        dm = FakeChannel(42)
        dm_message = FakeReplyMessage(dm)
        bot._queue(dm_message, "hey", [])
        await asyncio.sleep(0.3)
        assert dm_message.replied == []

        guild_channel = FakeChannel(43)
        guild_channel.guild = SimpleNamespace(id=7)
        guild_message = FakeReplyMessage(guild_channel)
        bot._queue(guild_message, "hey", [])
        await asyncio.sleep(0.3)
        assert guild_message.replied == ["part one"]

        await bot.close()

    asyncio.run(scenario())


def test_messages_without_a_reply_button_are_sent_plainly(tmp_path):
    async def scenario():
        bot = _reply_bot(tmp_path)
        channel = FakeChannel(42)
        plain = SimpleNamespace(
            channel=channel, author=FakeAuthor(), id=900
        )

        bot._queue(plain, "hey", [])
        await asyncio.sleep(0.3)

        assert channel.sent == ["part one", "part two"]
        await bot.close()

    asyncio.run(scenario())
