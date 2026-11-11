"""Compress old conversation into a summary plus durable facts."""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from .humanize import estimate_tokens
from .llm import LlamaClient
from .memory import MemoryStore

log = logging.getLogger("nora.consolidate")

SYSTEM_PROMPT = """you are a memory clerk. you read a conversation and write down \
what should be remembered later.

output only lines in exactly these three formats, nothing else:
summary: <one sentence about what happened and how he seemed>
fact: <one durable thing about him>
event: <YYYY-MM-DD> | <something that happens on one single day>

rules:
- only write facts that were actually stated in the transcript. never guess, infer or invent a hobby, a game, a show, a person, a plan or an event. if it is not in the transcript, leave it out.
- only record things he said or clearly confirmed. never treat nora's questions, jokes, suggestions or assumptions as facts about him.
- the summary describes what actually happened. do not present nora's suggestions as plans unless he clearly agreed.
- a fact has to still be true next month. never write "tomorrow", "tonight", "next day", "this week" or "in two days" inside a fact, and never write a fact about one specific day that passes.
- never use vague time words either: "recently", "lately", "soon", "coming up", "the other day". they are wrong within a week. if the time matters, it belongs on an event line; if it does not, leave it out.
- anything that happens on one day is an event, not a fact: an exam, a test, an interview, a meeting, a deadline, a class, an appointment, a trip, a party, a flight. put it on an event line with the real calendar date, worked out from the [timestamp] at the start of the transcript line.
- events only when he stated them, and always with a real date. if you cannot work out the date, leave the event out rather than guessing.
- birthdays and anniversaries are facts, not events.
- facts are short, third person, about the person she is talking to (use their name when you know it)
- include long term things: names, people in their life, worries, health, where he lives, what he is studying or working on
- include tastes and routines only when he actually mentioned them: favourite games, shows, food, hobbies, what he does not like, what time he usually sleeps or wakes up
- skip small talk, jokes, present-tense states ("he is currently studying", "he is tired right now") and plans for later tonight. none of those are things worth remembering tomorrow.
- at most 6 fact lines, at most 4 event lines, then exactly 1 summary line
- no headings, no bullets, no explanations"""

_EVENT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*(?:\||-|:)?\s*(.+)$")


def _transcript(messages: list[dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        stamp = str(m.get("created_at") or "")[:16].replace("T", " ")
        speaker = "nora" if m["role"] == "assistant" else (m.get("author_name") or "him")
        lines.append(f"[{stamp}] {speaker}: {m['content']}")
    return "\n".join(lines)


def _parse_event(value: str) -> tuple[str, str] | None:
    """`2026-11-05 | exam at 2pm` -> ("exam at 2pm", "2026-11-05")."""
    match = _EVENT_RE.match(value.strip())
    if not match:
        return None
    try:
        when = date.fromisoformat(match.group(1))
    except ValueError:
        return None
    content = match.group(2).strip(" |")
    if not 4 < len(content) <= 220:
        return None
    return content, when.isoformat()


def parse_output(
    text: str,
) -> tuple[str, list[str], list[tuple[str, str]]]:
    """(summary, facts, events) where an event is (content, iso date)."""
    summaries: list[str] = []
    facts: list[str] = []
    events: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*•> ").strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("summary:"):
            value = line.split(":", 1)[1].strip()
            if value:
                summaries.append(value)
        elif lowered.startswith("event:"):
            parsed = _parse_event(line.split(":", 1)[1])
            if parsed is not None:
                events.append(parsed)
        elif lowered.startswith("fact:"):
            value = line.split(":", 1)[1].strip()
            if 4 < len(value) <= 220:
                facts.append(value)
    return " ".join(summaries).strip(), facts, events


async def consolidate(
    store: MemoryStore,
    client: LlamaClient,
    scope: str,
    *,
    batch: int = 30,
    min_messages: int = 8,
) -> dict[str, Any] | None:
    """Summarize the next chunk of unconsolidated messages for a scope."""
    last_id = store.last_summary_msg_id(scope)
    messages = store.messages_after(scope, last_id, batch)
    if len(messages) < min_messages:
        return None

    transcript = _transcript(messages)
    if estimate_tokens(transcript) > 2400:
        # Keep the newest part; summaries chain forward anyway.
        transcript = transcript[-9000:]

    try:
        output = await client.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            temperature=0.25,
            top_p=0.9,
            max_tokens=320,
        )
    except Exception:
        log.exception("consolidation request failed for scope %s", scope)
        return None

    summary, facts, events = parse_output(output)
    if not summary:
        log.warning("no summary parsed, skipping: %r", output[:200])
        return None

    last = messages[-1]["id"]
    store.add_summary(scope, summary, last)
    added_facts = sum(
        1
        for fact in facts
        if store.add_fact(scope, fact, kind="fact", source_id=last)
    )

    # An event that already happened is not worth remembering as upcoming.
    today = _day_of(messages[-1].get("created_at"))
    added_events = 0
    for content, when in events:
        try:
            day = date.fromisoformat(when)
        except ValueError:
            continue
        if day < today:
            continue
        if store.add_fact(
            scope, content, kind="event", source_id=last, event_on=day
        ):
            added_events += 1

    store.prune_events(today=today)
    log.info(
        "consolidated %d messages -> 1 summary, %d facts, %d events",
        len(messages),
        added_facts,
        added_events,
    )
    return {
        "summary": summary,
        "facts": added_facts + added_events,
        "messages": len(messages),
    }


def _day_of(stamp: Any) -> date:
    try:
        return date.fromisoformat(str(stamp)[:10])
    except (TypeError, ValueError):
        return date.today()
