from __future__ import annotations

import asyncio
import io
from pathlib import Path

from PIL import Image

from nora import config
from nora.brain import Nora
from nora.consolidate import consolidate, parse_output
from nora.memory import MemoryStore
from nora.persona import build_system_prompt

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def png_bytes(size=(16, 16)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buffer, "PNG")
    return buffer.getvalue()


def test_consolidation_prompt_only_records_stated_facts():
    from nora.consolidate import SYSTEM_PROMPT

    assert "only write facts that were actually stated" in SYSTEM_PROMPT
    assert "never treat nora's questions" in SYSTEM_PROMPT
    assert "do not present nora's suggestions as plans" in SYSTEM_PROMPT


def test_parse_output_from_lines():
    summary, facts = parse_output(
        "summary: he survived week one\n"
        "fact: he works at a warehouse\n"
        "- fact: his manager talks fast\n"
        "random noise here"
    )
    assert summary == "he survived week one"
    assert facts == ["he works at a warehouse", "his manager talks fast"]


def test_parse_output_rejects_short_facts():
    _, facts = parse_output("fact: hi")
    assert facts == []


class FakeClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return self.reply


class RecordingClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.messages: list[list[dict[str, str]]] = []

    async def chat(self, messages, **kwargs):
        self.messages.append(messages)
        return self.reply


def test_consolidate_stores_summary_and_facts(tmp_path):
    store = MemoryStore(tmp_path / "n.db")
    for i in range(10):
        store.add_message("dm:1", "user", f"message number {i}")
    client = FakeClient("summary: they talked about work\nfact: he started a new job")

    result = asyncio.run(consolidate(store, client, "dm:1", batch=10))

    assert result == {"summary": "they talked about work", "facts": 1, "messages": 10}
    assert store.summaries("dm:1", 5) == ["they talked about work"]
    assert [f["content"] for f in store.facts("dm:1")] == ["he started a new job"]
    # the stranger with a different scope sees nothing
    assert store.facts("u:999") == []


def test_consolidate_skips_small_batches(tmp_path):
    store = MemoryStore(tmp_path / "n.db")
    store.add_message("dm:1", "user", "one")
    client = FakeClient("summary: x")
    assert asyncio.run(consolidate(store, client, "dm:1", batch=10)) is None
    assert client.calls == 0


def test_consolidate_does_not_advance_without_summary(tmp_path):
    store = MemoryStore(tmp_path / "n.db")
    for i in range(10):
        store.add_message("dm:1", "user", f"message {i}")
    client = FakeClient("i do not want to play this game")
    assert asyncio.run(consolidate(store, client, "dm:1", batch=10)) is None
    assert store.last_summary_msg_id("dm:1") == 0


def test_build_messages_includes_persona_memory_and_history(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    store = MemoryStore(settings.path(settings.memory.db_path))
    store.add_fact("dm:1", "he works at the airport now")
    store.add_message("dm:1", "user", "my manager talks fast lol")
    store.add_message("dm:1", "assistant", "arch btw")

    nora = Nora(settings, store=store)
    messages = nora.build_messages("dm:1")

    assert messages[0]["role"] == "system"
    system = messages[0]["content"]
    assert "he works at the airport now" in system
    assert "arch btw" not in system
    assert messages[-1] == {"role": "assistant", "content": "arch btw"}
    assert messages[-2]["content"] == "my manager talks fast lol"


def test_prompt_includes_a_mood(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "moodprompt.db")
    store = MemoryStore(settings.path(settings.memory.db_path))
    store.add_message("u:5", "user", "hey")

    system = Nora(settings, store=store).build_messages("u:5")[0]["content"]

    assert "# your mood right now" in system


def test_guild_history_gets_speaker_labels(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    store = MemoryStore(settings.path(settings.memory.db_path))
    store.add_message("ch:9", "user", "hello", author_name="mark")

    messages = Nora(settings, store=store).build_messages("ch:9", label_speakers=True)
    assert messages[-1]["content"] == "mark: hello"


def test_trim_respects_token_budget(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    settings.chat.history_token_budget = 40
    store = MemoryStore(settings.path(settings.memory.db_path))
    for i in range(20):
        store.add_message("dm:1", "user", "x" * 200)

    nora = Nora(settings, store=store)
    messages = nora.build_messages("dm:1")
    assert 2 <= len(messages) <= 4


def test_system_prompt_contains_facts_and_time():
    settings = config.load()
    prompt = build_system_prompt(
        settings, facts=["he hates mondays"], summaries=["they talked about work"]
    )
    assert "he hates mondays" in prompt
    assert "they talked about work" in prompt
    assert "right now" in prompt


def test_system_prompt_mentions_long_silence():
    settings = config.load()
    prompt = build_system_prompt(settings, facts=[], summaries=[], idle_minutes=60 * 24 * 5)
    assert "days since you two last texted" in prompt


def test_system_prompt_stays_quiet_for_short_gaps():
    settings = config.load()
    prompt = build_system_prompt(settings, facts=[], summaries=[], idle_minutes=5)
    assert "since you two last texted" not in prompt


def test_prompt_says_when_nothing_is_known():
    settings = config.load()
    prompt = build_system_prompt(settings, facts=[], summaries=[])
    assert "you do not know much about him yet" in prompt
    assert "do not pretend you know them" in prompt


def test_prompt_does_not_say_nothing_is_known_when_facts_exist():
    settings = config.load()
    prompt = build_system_prompt(settings, facts=["he likes chess"], summaries=[])
    assert "he likes chess" in prompt
    assert "you do not know much about him yet" not in prompt


def test_proactive_prompt_forbids_invented_memories():
    settings = config.load()
    prompt = build_system_prompt(settings, facts=[], summaries=[], proactive=True)
    assert "never invent a memory" in prompt
    assert "a hobby, a game, a show" in prompt
    assert "do not pretend to know" in prompt


def test_timetable_and_calendar_reach_the_prompt(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    settings.timetable.enabled = True
    settings.calendar.enabled = True
    settings.timetable.file = str(FIXTURES / "timetable.yaml")
    settings.calendar.file = str(FIXTURES / "calendar.yaml")
    settings.persona.owner_id = 123456789012345678
    store = MemoryStore(settings.path(settings.memory.db_path))
    owner = "u:123456789012345678"
    store.add_message(owner, "user", "hey")

    system = Nora(settings, store=store).build_messages(owner)[0]["content"]

    assert "his class timetable" in system
    assert "monday: 11:00-12:00 CS 101 (T)" in system
    assert "# right now" in system


def test_other_people_do_not_get_the_schedule(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    settings.timetable.enabled = True
    settings.calendar.enabled = True
    settings.timetable.file = str(FIXTURES / "timetable.yaml")
    settings.calendar.file = str(FIXTURES / "calendar.yaml")
    settings.persona.owner_id = 123456789012345678
    store = MemoryStore(settings.path(settings.memory.db_path))
    store.add_message("u:555", "user", "hey")

    system = Nora(settings, store=store).build_messages("u:555")[0]["content"]

    assert "his class timetable" not in system
    assert "CS 101" not in system
    # but she still treats them like her boyfriend
    assert "his girlfriend" in system


def test_photo_is_sent_as_multimodal_parts(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    projector = tmp_path / "mmproj-test.gguf"
    projector.write_bytes(b"x")
    settings.model.mmproj = str(projector)

    store = MemoryStore(settings.path(settings.memory.db_path))
    client = RecordingClient("nice shoes")
    nora = Nora(settings, store=store, client=client)
    assert nora.vision_enabled is True

    chunks = asyncio.run(nora.reply("dm:1", "look at this", images=[png_bytes()]))

    assert chunks == ["nice shoes"]
    sent = client.messages[-1]
    content = sent[-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look at this"}
    assert any(part["type"] == "image_url" for part in content)
    assert "he just sent you a picture" in sent[0]["content"]
    assert any("sent a photo" in m["content"] for m in store.recent("dm:1", 5))


def test_photo_without_a_projector_stays_text_only(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    settings.model.mmproj = None
    settings.model.gguf_dir = str(tmp_path / "empty")

    store = MemoryStore(settings.path(settings.memory.db_path))
    client = RecordingClient("i cant see it")
    nora = Nora(settings, store=store, client=client)
    assert nora.vision_enabled is False

    asyncio.run(nora.reply("dm:1", "look", images=[png_bytes()]))

    sent = client.messages[-1]
    assert isinstance(sent[-1]["content"], str)
    assert "he just sent you a picture" not in sent[0]["content"]
    # the fact that a picture happened is still remembered
    assert any("sent a photo" in m["content"] for m in store.recent("dm:1", 5))


def test_photo_without_text_still_replies(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    settings.model.mmproj = None
    settings.model.gguf_dir = str(tmp_path / "empty")
    store = MemoryStore(settings.path(settings.memory.db_path))
    nora = Nora(settings, store=store, client=RecordingClient("hm"))

    chunks = asyncio.run(nora.reply("dm:1", "", images=[png_bytes()]))
    assert chunks == ["hm"]


def test_proactive_prompt_asks_her_to_start_it():
    settings = config.load()
    prompt = build_system_prompt(
        settings, facts=[], summaries=[], idle_minutes=500, proactive=True
    )
    assert "right now it is on you" in prompt
    assert "answer the last message only" not in prompt

def test_initiate_generates_and_stores_openers(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    store = MemoryStore(settings.path(settings.memory.db_path))
    store.add_message("dm:1", "user", "i have a dentist thing on friday")

    client = RecordingClient("you never told me how the dentist went")
    nora = Nora(settings, store=store, client=client)

    chunks = asyncio.run(nora.initiate("dm:1"))

    assert chunks == ["you never told me how the dentist went"]
    sent = client.messages[-1]
    assert sent[-1]["role"] == "user"
    assert "start it yourself" in sent[-1]["content"]
    assert "right now it is on you" in sent[0]["content"]
    assert store.recent("dm:1", 5)[-1]["role"] == "assistant"


def test_follow_up_asks_where_he_went(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    store = MemoryStore(settings.path(settings.memory.db_path))
    store.add_message("u:5", "user", "im home")
    store.add_message("u:5", "assistant", "you gonna eat")
    client = RecordingClient("hellooo")
    nora = Nora(settings, store=store, client=client)

    chunks = asyncio.run(nora.initiate("u:5", follow_up=True))

    assert chunks == ["hellooo"]
    sent = client.messages[-1]
    assert "he went quiet" in sent[0]["content"]
    assert "go find him" in sent[-1]["content"]
    assert "answer the last message only" not in sent[0]["content"]


def test_second_follow_up_is_worried(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    store = MemoryStore(settings.path(settings.memory.db_path))
    store.add_message("u:5", "user", "im home")
    store.add_message("u:5", "assistant", "you gonna eat")
    client = RecordingClient("hey you ok?")
    nora = Nora(settings, store=store, client=client)

    asyncio.run(nora.initiate("u:5", follow_up=True, worry_level=2))

    sent = client.messages[-1]
    assert "starting to worry" in sent[0]["content"]
    assert "getting worried" in sent[-1]["content"]
    assert "he went quiet" not in sent[0]["content"]


def test_keep_alive_nudge_when_she_only_reacts(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    store = MemoryStore(settings.path(settings.memory.db_path))
    store.add_message("u:5", "user", "hey")
    store.add_message("u:5", "assistant", "ugh")
    store.add_message("u:5", "assistant", "what")

    system = Nora(settings, store=store).build_messages("u:5")[0]["content"]
    assert "keep it alive" in system


def test_no_keep_alive_nudge_right_after_a_question(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    store = MemoryStore(settings.path(settings.memory.db_path))
    store.add_message("u:5", "user", "hey")
    store.add_message("u:5", "assistant", "what?")

    system = Nora(settings, store=store).build_messages("u:5")[0]["content"]
    assert "keep it alive" not in system


def test_keep_alive_can_be_turned_off(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    settings.chat.keep_conversation = False
    store = MemoryStore(settings.path(settings.memory.db_path))
    store.add_message("u:5", "user", "hey")
    store.add_message("u:5", "assistant", "ugh")

    system = Nora(settings, store=store).build_messages("u:5")[0]["content"]
    assert "keep it alive" not in system


def test_each_person_gets_their_own_memory_file(tmp_path):
    settings = config.load()
    settings.memory.dir = str(tmp_path / "people")
    settings.memory.db_path = str(tmp_path / "shared.db")
    nora = Nora(settings)
    try:
        first = nora.store_for("u:111111111111111111")
        second = nora.store_for("u:222222222222222222")

        assert first.path != second.path
        assert first.path.name == "111111111111111111.db"
        assert second.path.name == "222222222222222222.db"
        assert first.path.parent == tmp_path / "people"

        first.add_message("u:111111111111111111", "user", "a secret for the first")
        second.add_message("u:222222222222222222", "user", "a secret for the second")

        assert [
            m["content"] for m in first.recent("u:111111111111111111", 5)
        ] == ["a secret for the first"]
        assert [
            m["content"] for m in second.recent("u:222222222222222222", 5)
        ] == ["a secret for the second"]
        # the other file knows nothing about it
        assert first.recent("u:222222222222222222", 5) == []
        assert second.recent("u:111111111111111111", 5) == []

        # one file per person, reused every time
        assert nora.store_for("u:111111111111111111") is first
    finally:
        asyncio.run(nora.close())


def test_memory_files_survive_a_restart(tmp_path):
    settings = config.load()
    settings.memory.dir = str(tmp_path / "people")
    settings.memory.db_path = str(tmp_path / "shared.db")

    first = Nora(settings)
    first.store_for("u:42").add_fact("u:42", "he likes tea")
    asyncio.run(first.close())

    second = Nora(settings)
    try:
        assert [f["content"] for f in second.store_for("u:42").facts("u:42")] == [
            "he likes tea"
        ]
        assert second.store_for("u:43").facts("u:43") == []
    finally:
        asyncio.run(second.close())


def test_terminal_client_uses_the_shared_file(tmp_path):
    settings = config.load()
    settings.memory.dir = str(tmp_path / "people")
    settings.memory.db_path = str(tmp_path / "shared.db")
    nora = Nora(settings)
    try:
        assert nora.store_for("cli:local").path == tmp_path / "shared.db"
    finally:
        asyncio.run(nora.close())


def test_compose_does_not_store_her_side(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    store = MemoryStore(settings.path(settings.memory.db_path))
    nora = Nora(settings, store=store, client=RecordingClient("hm"))

    chunks = asyncio.run(nora.compose("u:5", "hi"))

    assert chunks == ["hm"]
    assert [m["role"] for m in store.recent("u:5", 5)] == ["user"]

    nora.record_reply("u:5", chunks)
    assert [m["role"] for m in store.recent("u:5", 5)] == ["user", "assistant"]


def test_adjust_block_only_when_she_retries(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    store = MemoryStore(settings.path(settings.memory.db_path))
    client = RecordingClient("hm")
    nora = Nora(settings, store=store, client=client)

    asyncio.run(nora.compose("u:5", "hi", adjust=True))
    assert "he kept typing" in client.messages[-1][0]["content"]

    asyncio.run(nora.compose("u:5", "hi again"))
    assert "he kept typing" not in client.messages[-1][0]["content"]


def test_initiate_returns_nothing_when_the_model_is_silent(tmp_path):
    settings = config.load()
    settings.memory.db_path = str(tmp_path / "n.db")
    store = MemoryStore(settings.path(settings.memory.db_path))
    nora = Nora(settings, store=store, client=RecordingClient(""))

    assert asyncio.run(nora.initiate("dm:1")) == []
    assert store.recent("dm:1", 5) == []
