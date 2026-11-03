from __future__ import annotations

import sqlite3

import pytest

from nora.memory import MemoryStore, normalize_fact


@pytest.fixture()
def store(tmp_path):
    db = MemoryStore(tmp_path / "nora.db")
    yield db
    db.close()


def test_messages_roundtrip(store):
    store.add_message("u:1", "user", "first week done", author_name="mark")
    store.add_message("u:1", "assistant", "oh so you survived")
    rows = store.recent("u:1", 10)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["author_name"] == "mark"


def test_recent_is_chronological_and_capped(store):
    for i in range(10):
        store.add_message("u:1", "user", f"line {i}")
    rows = store.recent("u:1", 3)
    assert [r["content"] for r in rows] == ["line 7", "line 8", "line 9"]


def test_scopes_are_isolated(store):
    store.add_message("u:1", "user", "secret")
    store.add_message("u:2", "user", "other")
    assert len(store.recent("u:1", 10)) == 1
    assert store.recent("u:2", 10)[0]["content"] == "other"


def test_count_since(store):
    first = store.add_message("u:1", "user", "a")
    store.add_message("u:1", "user", "b")
    store.add_message("u:1", "user", "c")
    assert store.count_since("u:1", first) == 2


def test_idle_minutes(store):
    assert store.idle_minutes("u:1") is None
    store.add_message("u:1", "user", "hi")
    assert store.idle_minutes("u:1") is not None


def test_kv_roundtrip(store):
    assert store.kv_get("mood") is None
    store.kv_set("mood", "warm")
    store.kv_set("mood", "playful")
    assert store.kv_get("mood") == "playful"


def test_user_idle_ignores_her_messages(store):
    store.add_message("u:1", "assistant", "hey")
    assert store.user_idle_minutes("u:1") is None
    store.add_message("u:1", "user", "hi")
    value = store.user_idle_minutes("u:1")
    assert value is not None and value < 1


def test_user_idle_minutes_measures_the_gap(store):
    from datetime import datetime, timedelta

    store.add_message("u:1", "user", "hi")
    old = (datetime.now() - timedelta(hours=4)).isoformat(timespec="seconds")
    store._db.execute(
        "UPDATE messages SET created_at = ? WHERE scope = ?", (old, "u:1")
    )
    store._db.commit()

    value = store.user_idle_minutes("u:1")
    assert value is not None
    assert 239 <= value <= 241


def test_search_matches_words(store):
    store.add_message("u:1", "user", "my manager talks sooo fast at the new job")
    store.add_message("u:1", "user", "i bought milk")
    hits = store.search("manager job", limit=5, scope="u:1")
    assert hits
    assert "manager" in hits[0]["content"]


def test_search_is_scoped_per_person(store):
    store.add_message("u:1", "user", "my secret dentist thing")
    store.add_message("u:2", "user", "my secret dentist thing")
    hits = store.search("dentist", limit=5, scope="u:2")
    assert len(hits) == 1
    assert hits[0]["scope"] == "u:2"


def test_search_ignores_stopwords(store):
    store.add_message("u:1", "user", "the and you")
    assert store.search("the and you") == []


def test_search_can_exclude_ids(store):
    mid = store.add_message("u:1", "user", "the airport job thing")
    store.add_message("u:1", "user", "airport again")
    hits = store.search("airport", exclude_ids=[mid])
    assert all(h["id"] != mid for h in hits)


# -- facts ---------------------------------------------------------------
def test_facts_are_deduped(store):
    assert store.add_fact("u:1", "he started a job at the airport")
    assert store.add_fact("u:1", "He started a job at the airport!")
    facts = store.facts("u:1")
    assert len(facts) == 1
    assert facts[0]["salience"] > 1.0


def test_facts_stay_with_their_person(store):
    store.add_fact("u:1", "he has a dentist appointment friday")
    store.add_fact("u:2", "she hates mondays")

    assert [f["content"] for f in store.facts("u:1")] == [
        "he has a dentist appointment friday"
    ]
    assert [f["content"] for f in store.facts("u:2")] == ["she hates mondays"]


def test_same_fact_for_two_people_is_allowed(store):
    assert store.add_fact("u:1", "he likes tea")
    assert store.add_fact("u:2", "he likes tea")
    assert len(store.facts("u:1")) == 1
    assert len(store.facts("u:2")) == 1


def test_forget_fact(store):
    store.add_fact("u:1", "he hates mornings")
    fact_id = store.facts("u:1")[0]["id"]
    assert store.forget_fact(fact_id, scope="u:1")
    assert store.facts("u:1") == []


def test_forget_fact_cannot_touch_someone_else(store):
    store.add_fact("u:1", "he hates mornings")
    fact_id = store.facts("u:1")[0]["id"]
    assert store.forget_fact(fact_id, scope="u:2") is False
    assert len(store.facts("u:1")) == 1


def test_summaries_chain(store):
    msg = store.add_message("u:1", "user", "hi")
    store.add_summary("u:1", "they caught up", msg)
    assert store.last_summary_msg_id("u:1") == msg
    assert store.summaries("u:1", 5) == ["they caught up"]


def test_forget_scope_clears_thread(store):
    msg = store.add_message("u:1", "user", "hi")
    store.add_summary("u:1", "x", msg)
    store.add_fact("u:1", "he likes tea")
    assert store.forget_scope("u:1") == 1
    assert store.recent("u:1", 5) == []
    assert store.summaries("u:1", 5) == []
    assert store.facts("u:1") == []


def test_normalize_fact():
    assert normalize_fact("He works at the Airport!") == "he works at the airport"


def test_stats(store):
    store.add_message("u:1", "user", "hi")
    store.add_fact("u:1", "he likes tea")
    assert store.stats() == {"messages": 1, "facts": 1, "summaries": 0}


def test_scope_stats(store):
    store.add_message("u:1", "user", "hi")
    store.add_message("u:2", "user", "hi")
    store.add_fact("u:2", "she likes tea")
    assert store.scope_stats("u:2") == {"messages": 1, "facts": 1}


def _insert_at(store: MemoryStore, scope: str, hour: int, text: str = "hi") -> None:
    store._db.execute(
        "INSERT INTO messages (scope, role, content, created_at)"
        " VALUES (?, 'user', ?, ?)",
        (scope, text, f"2026-11-01T{hour:02d}:00:00"),
    )
    store._db.commit()


def test_quiet_window_is_learned_from_message_times(store):
    for hour in list(range(8, 24)) + [0]:
        for _ in range(3):
            _insert_at(store, "u:7", hour)

    assert store.quiet_window("u:7") == (1, 7)


def test_quiet_window_needs_history(store):
    _insert_at(store, "u:7", 15)
    assert store.quiet_window("u:7") is None


def test_quiet_window_is_none_when_he_never_sleeps(store):
    for hour in range(24):
        for _ in range(3):
            _insert_at(store, "u:7", hour)
    assert store.quiet_window("u:7") is None


def test_routine_reaches_the_prompt(tmp_path):
    from nora import config
    from nora.brain import Nora

    settings = config.load()
    settings.memory.db_path = str(tmp_path / "routine.db")
    local = MemoryStore(settings.path(settings.memory.db_path))
    for hour in list(range(8, 24)) + [0]:
        for _ in range(3):
            _insert_at(local, "u:5", hour)
    local.add_message("u:5", "user", "hey")

    system = Nora(settings, store=local).build_messages("u:5")[0]["content"]
    assert "his routine" in system
    assert "usually asleep between 1am and 7am" in system


# -- migrations ----------------------------------------------------------
def test_old_facts_table_is_migrated(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(
        """
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            normalized TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'fact',
            salience REAL NOT NULL DEFAULT 1.0,
            source_id INTEGER,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        INSERT INTO facts (normalized, content, kind, salience, created_at, last_seen_at)
        VALUES ('he likes tea', 'he likes tea', 'fact', 1.0, '2026-10-01T10:00:00',
                '2026-10-01T10:00:00');
        """
    )
    raw.commit()
    raw.close()

    store = MemoryStore(path, legacy_scope="u:741")
    try:
        facts = store.facts("u:741")
        assert [f["content"] for f in facts] == ["he likes tea"]
    finally:
        store.close()


def test_old_dm_threads_become_per_person(tmp_path):
    path = tmp_path / "legacy2.db"
    store = MemoryStore(path)
    store.add_message("dm:555", "user", "hello there", author_id=42)
    store.add_summary("dm:555", "they met", 1)
    store.close()

    reopened = MemoryStore(path)
    try:
        rows = reopened.recent("u:42", 10)
        assert [r["content"] for r in rows] == ["hello there"]
        assert reopened.summaries("u:42", 5) == ["they met"]
        assert reopened.recent("dm:555", 10) == []
    finally:
        reopened.close()
