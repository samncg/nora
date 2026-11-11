"""Long-term memory: full transcript, keyword recall, durable facts, summaries.

Every conversation is scoped to the person she is talking to (`u:<discord id>`),
so each person gets their own history, facts and summaries.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import dates

log = logging.getLogger("nora.memory")

FACTS_TABLE = """
CREATE TABLE IF NOT EXISTS facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT    NOT NULL DEFAULT '',
    normalized   TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    kind         TEXT    NOT NULL DEFAULT 'fact',
    salience     REAL    NOT NULL DEFAULT 1.0,
    source_id    INTEGER,
    created_at   TEXT    NOT NULL,
    last_seen_at TEXT    NOT NULL,
    -- events only: the day it happens, so they can stop being true
    event_on     TEXT,
    UNIQUE(scope, normalized)
);
"""

FACTS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_facts_scope ON facts(scope, last_seen_at)"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    author_id   TEXT,
    author_name TEXT,
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages(scope, id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS summaries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    to_msg_id  INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_scope ON summaries(scope, id);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

STOPWORDS = {
    "the", "and", "you", "your", "yours", "for", "was", "were", "are", "his",
    "her", "him", "she", "they", "them", "that", "this", "with", "have", "has",
    "had", "but", "not", "what", "when", "where", "who", "why", "how", "from",
    "just", "about", "into", "then", "than", "been", "will", "would", "could",
    "should", "can", "cant", "dont", "didnt", "doesnt", "yeah", "lol", "like",
    "okay", "ok", "got", "get", "now", "out", "one", "some", "really", "very",
    "know", "think", "there", "here", "im", "ive", "its", "it's", "a", "an",
}

_QUOTE_RE = re.compile(r"[^a-z0-9 ]+")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_fact(text: str) -> str:
    cleaned = _QUOTE_RE.sub(" ", text.lower())
    return " ".join(cleaned.split())


def _base_date(stamp: str | None) -> date:
    try:
        return date.fromisoformat(str(stamp)[:10])
    except (TypeError, ValueError):
        return date.today()


class MemoryStore:
    """A tiny, thread-safe wrapper around the Nora database."""

    def __init__(self, path: str | Path, *, legacy_scope: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.executescript(FACTS_TABLE)
            self._migrate(legacy_scope)
            self._migrate_event_on()
            self._db.execute(FACTS_INDEX)
            self._db.commit()
        self.maintain_facts()

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- key/value -------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._db.commit()

    # -- migrations ------------------------------------------------------
    def _migrate(self, legacy_scope: str | None) -> None:
        self._migrate_facts_table()
        self._migrate_dm_scopes()
        if legacy_scope:
            # Facts created before per-person scopes belong to the owner.
            self._db.execute(
                "UPDATE facts SET scope = ? WHERE scope = ''", (legacy_scope,)
            )

    def _migrate_facts_table(self) -> None:
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(facts)")}
        if "scope" in columns:
            return
        log.info("migrating facts to per-person scopes")
        self._db.execute("ALTER TABLE facts RENAME TO facts_legacy")
        self._db.executescript(FACTS_TABLE)
        for row in self._db.execute("SELECT * FROM facts_legacy").fetchall():
            self._db.execute(
                "INSERT OR IGNORE INTO facts (id, scope, normalized, content, kind,"
                " salience, source_id, created_at, last_seen_at)"
                " VALUES (?, '', ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row["normalized"],
                    row["content"],
                    row["kind"],
                    row["salience"],
                    row["source_id"],
                    row["created_at"],
                    row["last_seen_at"],
                ),
            )
        self._db.execute("DROP TABLE facts_legacy")

    def _migrate_event_on(self) -> None:
        """Databases created before events had dates need the new column."""
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(facts)")}
        if "event_on" in columns:
            return
        log.info("adding event_on to facts")
        self._db.execute("ALTER TABLE facts ADD COLUMN event_on TEXT")
        self._db.commit()

    def _migrate_dm_scopes(self) -> None:
        """Old `dm:<channel>` threads become `u:<person>` threads."""
        scopes = [
            row["scope"]
            for row in self._db.execute(
                "SELECT DISTINCT scope FROM messages WHERE scope LIKE 'dm:%'"
            )
        ]
        migrated = 0
        for scope in scopes:
            owner = self._db.execute(
                "SELECT author_id, COUNT(*) AS n FROM messages"
                " WHERE scope = ? AND author_id IS NOT NULL"
                " GROUP BY author_id ORDER BY n DESC LIMIT 1",
                (scope,),
            ).fetchone()
            if not owner or not owner["author_id"]:
                continue
            target = f"u:{owner['author_id']}"
            self._db.execute(
                "UPDATE messages SET scope = ? WHERE scope = ?", (target, scope)
            )
            self._db.execute(
                "UPDATE summaries SET scope = ? WHERE scope = ?", (target, scope)
            )
            migrated += 1
        if migrated:
            log.info("moved %d dm conversation(s) to per-person scopes", migrated)

    # -- messages --------------------------------------------------------
    def add_message(
        self,
        scope: str,
        role: str,
        content: str,
        *,
        author_id: int | str | None = None,
        author_name: str | None = None,
    ) -> int:
        content = content.strip()
        if not content:
            return 0
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages (scope, role, author_id, author_name, content,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    scope,
                    role,
                    str(author_id) if author_id is not None else None,
                    author_name,
                    content,
                    _now(),
                ),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def recent(self, scope: str, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE scope = ?"
                " ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (scope, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def messages_after(
        self, scope: str, after_id: int, limit: int
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE scope = ? AND id > ?"
                " ORDER BY id ASC LIMIT ?",
                (scope, after_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_since(self, scope: str, after_id: int) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE scope = ? AND id > ?",
                (scope, after_id),
            ).fetchone()
        return int(row["n"])

    def idle_minutes(self, scope: str) -> float | None:
        """Minutes since the last stored message in this scope."""
        with self._lock:
            row = self._db.execute(
                "SELECT created_at FROM messages WHERE scope = ?"
                " ORDER BY id DESC LIMIT 1",
                (scope,),
            ).fetchone()
        if not row:
            return None
        try:
            then = datetime.fromisoformat(row["created_at"])
        except ValueError:
            return None
        return max(0.0, (datetime.now() - then).total_seconds() / 60.0)

    def user_idle_minutes(self, scope: str) -> float | None:
        """Minutes since the last message from the user in this scope."""
        with self._lock:
            row = self._db.execute(
                "SELECT created_at FROM messages WHERE scope = ? AND role = 'user'"
                " ORDER BY id DESC LIMIT 1",
                (scope,),
            ).fetchone()
        if not row:
            return None
        try:
            then = datetime.fromisoformat(row["created_at"])
        except ValueError:
            return None
        return max(0.0, (datetime.now() - then).total_seconds() / 60.0)

    def quiet_window(
        self, scope: str, *, min_messages: int = 24, hours: int = 6
    ) -> tuple[int, int] | None:
        """Guess when he is normally asleep from when he actually texts.

        Returns (start_hour, end_hour) for the quietest stretch of the day, or
        None when there is not enough history to be sure.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT CAST(strftime('%H', created_at) AS INTEGER) AS h,"
                " COUNT(*) AS n FROM messages WHERE scope = ? AND role = 'user'"
                " GROUP BY h",
                (scope,),
            ).fetchall()
        total = sum(int(row["n"]) for row in rows)
        if total < min_messages:
            return None

        counts = [0] * 24
        for row in rows:
            counts[int(row["h"])] = int(row["n"])

        hours = min(max(3, hours), 12)
        best_start, best_score = 0, None
        for start in range(24):
            score = sum(counts[(start + offset) % 24] for offset in range(hours))
            if best_score is None or score < best_score:
                best_start, best_score = start, score

        average = total / 24 * hours
        if best_score is None or best_score > average * 0.35:
            return None  # he texts at all hours, no reliable pattern
        return best_start, (best_start + hours) % 24

    # -- recall ----------------------------------------------------------
    def search(
        self,
        query: str,
        limit: int = 5,
        *,
        scope: str | None = None,
        exclude_ids: Iterable[int] = (),
    ) -> list[dict[str, Any]]:
        match = self._fts_query(query)
        if not match:
            return []
        excluded = {int(i) for i in exclude_ids}
        sql = (
            "SELECT m.*, bm25(messages_fts) AS rank FROM messages_fts"
            " JOIN messages m ON m.id = messages_fts.rowid"
            " WHERE messages_fts MATCH ?"
        )
        params: list[Any] = [match]
        if scope:
            sql += " AND m.scope = ?"
            params.append(scope)
        sql += " ORDER BY rank LIMIT ?"
        params.append(max(limit * 4, limit))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        hits = [dict(r) for r in rows if int(r["id"]) not in excluded]
        return hits[:limit]

    @staticmethod
    def _fts_query(text: str) -> str:
        tokens = [
            t
            for t in re.findall(r"[a-z0-9']{3,}", text.lower())
            if t not in STOPWORDS
        ]
        # Keep the most "content-like" tokens, deduped, in order.
        seen: list[str] = []
        for t in tokens:
            if t not in seen:
                seen.append(t)
        seen = seen[-8:]
        return " OR ".join(f'"{t}"' for t in seen)

    # -- summaries -------------------------------------------------------
    def add_summary(self, scope: str, content: str, to_msg_id: int) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO summaries (scope, content, to_msg_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (scope, content.strip(), to_msg_id, _now()),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def last_summary_msg_id(self, scope: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(to_msg_id) AS m FROM summaries WHERE scope = ?", (scope,)
            ).fetchone()
        return int(row["m"] or 0)

    def summaries(self, scope: str, limit: int) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM (SELECT * FROM summaries WHERE scope = ?"
                " ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (scope, limit),
            ).fetchall()
        return [r["content"] for r in rows]

    # -- facts -----------------------------------------------------------
    def add_fact(
        self,
        scope: str,
        content: str,
        *,
        kind: str = "fact",
        source_id: int | None = None,
        event_on: str | date | None = None,
    ) -> bool:
        content = " ".join(content.strip().split())
        if len(content) < 4:
            return False
        normalized = normalize_fact(content)
        if not normalized:
            return False
        when = str(event_on)[:10] if event_on else None
        now = _now()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO facts (scope, normalized, content, kind, source_id,"
                " event_on, created_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(scope, normalized) DO UPDATE SET"
                "   last_seen_at = excluded.last_seen_at,"
                "   salience = MIN(facts.salience + 0.35, 5.0),"
                "   kind = excluded.kind,"
                "   event_on = COALESCE(excluded.event_on, facts.event_on)",
                (scope, normalized, content, kind, source_id, when, now, now),
            )
            self._db.commit()
            return cur.rowcount > 0

    def facts(
        self, scope: str, limit: int = 40, *, today: date | None = None
    ) -> list[dict[str, Any]]:
        """Current facts, newest first. Events stop counting once the day passes."""
        cutoff = (today or date.today()).isoformat()
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM facts WHERE scope = ?"
                " AND (event_on IS NULL OR event_on >= ?)"
                " ORDER BY last_seen_at DESC, id DESC LIMIT ?",
                (scope, cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def reclassify_dated_facts(self) -> int:
        """Turn old facts that describe a moment into expiring events.

        Earlier versions wrote `he has an exam tomorrow morning` or `he is
        currently studying` as plain facts, which then got repeated forever.
        This dates those rows so they stop being injected once the moment is
        over. Safe to run repeatedly.
        """
        changed = 0
        with self._lock:
            rows = self._db.execute(
                "SELECT id, content, created_at FROM facts"
                " WHERE kind = 'fact' AND event_on IS NULL"
            ).fetchall()
            for row in rows:
                base = _base_date(row["created_at"])
                resolved = dates.resolve(row["content"], base=base)
                if resolved is not None:
                    content, when = resolved
                elif dates.is_transient(row["content"]):
                    # no date, but it claimed to be true right now
                    content, when = row["content"], base
                else:
                    continue
                # OR IGNORE: the rewritten text may collide with another row
                self._db.execute(
                    "UPDATE OR IGNORE facts SET content = ?, normalized = ?,"
                    " kind = 'event', event_on = ? WHERE id = ?",
                    (content, normalize_fact(content), when.isoformat(), row["id"]),
                )
                changed += 1
            if changed:
                self._db.commit()
        if changed:
            log.info("dated %d old facts so they expire", changed)
        return changed

    def prune_events(self, *, today: date | None = None, days: int = 14) -> int:
        """Drop events that finished long ago; they are dead weight."""
        cutoff = ((today or date.today()) - timedelta(days=max(0, days))).isoformat()
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM facts WHERE kind = 'event' AND event_on IS NOT NULL"
                " AND event_on < ?",
                (cutoff,),
            )
            self._db.commit()
            return cur.rowcount

    def maintain_facts(self) -> None:
        """Keep dated facts honest every time a memory file is opened."""
        try:
            self.reclassify_dated_facts()
            self.prune_events()
        except Exception:
            log.warning("could not tidy the facts", exc_info=True)

    def forget_fact(self, fact_id: int, scope: str | None = None) -> bool:
        with self._lock:
            if scope is None:
                cur = self._db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            else:
                cur = self._db.execute(
                    "DELETE FROM facts WHERE id = ? AND scope = ?", (fact_id, scope)
                )
            self._db.commit()
            return cur.rowcount > 0

    def forget_all_facts(self, scope: str | None = None) -> int:
        with self._lock:
            if scope is None:
                cur = self._db.execute("DELETE FROM facts")
            else:
                cur = self._db.execute("DELETE FROM facts WHERE scope = ?", (scope,))
            self._db.commit()
            return cur.rowcount

    def forget_scope(self, scope: str) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM messages WHERE scope = ?", (scope,))
            self._db.execute("DELETE FROM summaries WHERE scope = ?", (scope,))
            self._db.execute("DELETE FROM facts WHERE scope = ?", (scope,))
            self._db.commit()
            return cur.rowcount

    # -- misc ------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        with self._lock:
            messages = self._db.execute(
                "SELECT COUNT(*) AS n FROM messages"
            ).fetchone()["n"]
            facts = self._db.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"]
            summaries = self._db.execute(
                "SELECT COUNT(*) AS n FROM summaries"
            ).fetchone()["n"]
        return {
            "messages": int(messages),
            "facts": int(facts),
            "summaries": int(summaries),
        }

    def scope_stats(self, scope: str) -> dict[str, int]:
        with self._lock:
            messages = self._db.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE scope = ?", (scope,)
            ).fetchone()["n"]
            facts = self._db.execute(
                "SELECT COUNT(*) AS n FROM facts WHERE scope = ?", (scope,)
            ).fetchone()["n"]
        return {"messages": int(messages), "facts": int(facts)}
