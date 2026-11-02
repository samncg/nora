"""Compress old conversation into a summary plus durable facts."""

from __future__ import annotations

import logging
from typing import Any

from .humanize import estimate_tokens
from .llm import LlamaClient
from .memory import MemoryStore

log = logging.getLogger("nora.consolidate")

SYSTEM_PROMPT = """you are a memory clerk. you read a conversation and write down \
what should be remembered later.

output only lines in exactly these two formats, nothing else:
summary: <one sentence about what happened and how he seemed>
fact: <one durable thing about him>

rules:
- facts are short, third person, about the person she is talking to (use their name when you know it)
- include names, dates, plans, worries, health, people in their life
- include tastes and routines too: favourite games, shows, food, hobbies, what he does not like, what time he usually sleeps or wakes up
- skip small talk, jokes and temporary states like "he is eating right now"
- at most 6 fact lines, then exactly 1 summary line
- no headings, no bullets, no explanations"""


def _transcript(messages: list[dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        stamp = str(m.get("created_at") or "")[:16].replace("T", " ")
        speaker = "nora" if m["role"] == "assistant" else (m.get("author_name") or "him")
        lines.append(f"[{stamp}] {speaker}: {m['content']}")
    return "\n".join(lines)


def parse_output(text: str) -> tuple[str, list[str]]:
    summaries: list[str] = []
    facts: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*•> ").strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("summary:"):
            value = line.split(":", 1)[1].strip()
            if value:
                summaries.append(value)
        elif lowered.startswith("fact:"):
            value = line.split(":", 1)[1].strip()
            if 4 < len(value) <= 220:
                facts.append(value)
    return " ".join(summaries).strip(), facts


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

    summary, facts = parse_output(output)
    if not summary:
        log.warning("no summary parsed, skipping: %r", output[:200])
        return None

    store.add_summary(scope, summary, messages[-1]["id"])
    added = sum(
        1
        for fact in facts
        if store.add_fact(scope, fact, kind="fact", source_id=messages[-1]["id"])
    )
    log.info("consolidated %d messages -> 1 summary, %d facts", len(messages), added)
    return {"summary": summary, "facts": added, "messages": len(messages)}
