"""Things he has coming up, so she can remind him before they start.

Extraction is deterministic on purpose. A small model misses dates often enough
that the reminder never happens, so this is plain regex over the message: one
event per message, stored per person, checked by the bot on a timer.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Any, Iterable

from .config import RemindersConfig
from .memory import MemoryStore

KV_KEY = "reminders"
MAX_REMINDERS = 60
MAX_PROMPT_LINES = 4
# A reminder that was never sent is not worth keeping forever.
KEEP_PAST_HOURS = 6.0

# (pattern, how she would say it). Longer phrases first so "viva" beats "exam".
EVENTS: tuple[tuple[str, str], ...] = (
    (r"\bexam(?:s)?\b", "exam"),
    (r"\bfinals?\b", "final"),
    (r"\bmidterm(?:s)?\b", "midterm"),
    (r"\btest\b", "test"),
    (r"\bquiz\b", "quiz"),
    (r"\bviva\b", "viva"),
    (r"\binterview\b", "interview"),
    (r"\bmeeting\b", "meeting"),
    (r"\bappointment\b", "appointment"),
    (r"\bdeadline\b", "deadline"),
    (r"\bassignment\b", "assignment"),
    (r"\bhomework\b", "homework"),
    (r"\bpresentation\b", "presentation"),
    (r"\bsubmission\b", "submission"),
    (r"\blab report\b", "lab report"),
    (r"\blab\b", "lab"),
    (r"\blecture\b", "lecture"),
    (r"\bclass\b", "class"),
    (r"\blesson\b", "lesson"),
    (r"\bseminar\b", "seminar"),
    (r"\bflight\b", "flight"),
    (r"\btrain\b", "train"),
    (r"\bbus\b", "bus"),
    (r"\bdoctor(?:'?s)?\b", "doctor appointment"),
    (r"\bdentist(?:'?s)?\b", "dentist appointment"),
    (r"\bcheckup\b", "checkup"),
    (r"\bdate\b", "date"),
    (r"\bbirthday\b", "birthday"),
    (r"\bparty\b", "party"),
    (r"\bconcert\b", "concert"),
    (r"\bgame\b", "game"),
    (r"\bmatch\b", "match"),
    (r"\btournament\b", "tournament"),
    (r"\bshift\b", "shift"),
    (r"\bdriving test\b", "driving test"),
)

_EVENT_RES = tuple((re.compile(pattern), label) for pattern, label in EVENTS)

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_TODAY_RE = re.compile(r"\b(today|tonight|this (?:morning|afternoon|evening))\b")
_TOMORROW_RE = re.compile(
    r"\b(tomorrow|tmr|tmrw|tmoz|tomoz|2moro|tommorow|tommorrow)\b"
)
_DAY_AFTER_RE = re.compile(r"\bday after tomorrow\b")
_WEEKDAY_RE = re.compile(
    r"\b(?:on\s+|this\s+|next\s+)?(" + "|".join(_WEEKDAYS) + r")\b"
)
_RELATIVE_RE = re.compile(
    r"\bin\s+(\d{1,3})\s*(minutes?|mins?|hours?|hrs?|h)\b"
)

_MERIDIEM_RE = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?\b")
_COLON_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_AT_RE = re.compile(r"\bat\s+(\d{1,2})\b(?!\s*[:.]?\d)")

_NAMED_TIMES: dict[str, tuple[int, int]] = {
    "midnight": (0, 0),
    "noon": (12, 0),
    "midday": (12, 0),
    "lunch": (12, 30),
    "morning": (9, 0),
    "afternoon": (14, 0),
    "evening": (19, 0),
    "tonight": (20, 0),
    "night": (21, 0),
}

# Words that mean he is talking about something that already happened.
_PAST_RE = re.compile(
    r"\b(was|were|had|finished|did|passed|failed|took|wrote)\b"
)
_YESTERDAY_RE = re.compile(r"\byesterday\b")
_NEGATION_RE = re.compile(
    r"\b(no|not|never|dont|don't|didnt|didn't|wont|won't|isnt|isn't|aint|"
    r"nothing|without)\b",
    re.I,
)

# Words that cannot be part of an event name ("my exam", "an exam").
_NOT_A_NAME = {
    "a", "an", "the", "my", "our", "your", "his", "her", "their", "this",
    "that", "these", "those", "have", "has", "had", "got", "im", "i", "m",
    "and", "but", "for", "with", "about", "of", "to", "in", "on", "at", "is",
    "are", "was", "were", "some", "any", "next", "big", "last", "silly",
}


@dataclass
class Reminder:
    """One thing he has coming up, stored as ISO strings so json is trivial."""

    title: str
    when: str
    lead_minutes: int = 30
    # when she actually texts him. picked at random inside the lead window so
    # it does not always land on the same offset before the event.
    notify_at: str | None = None
    created_at: str = ""
    notified_at: str | None = None

    @property
    def at(self) -> datetime:
        return datetime.fromisoformat(self.when)

    @property
    def notify(self) -> datetime:
        if self.notify_at:
            try:
                return datetime.fromisoformat(self.notify_at)
            except ValueError:
                pass
        return self.at - timedelta(minutes=max(0, self.lead_minutes))


def _normalise(text: str) -> str:
    text = (text or "").replace("\u2019", "'").lower()
    return " ".join(text.split())


def _clock(hour: int, minute: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    display = hour % 12 or 12
    if minute:
        return f"{display}:{minute:02d}{suffix}"
    return f"{display}{suffix}"


def _clean_label(label: str) -> str:
    label = " ".join(label.split()).lower()
    words = label.split()
    while words and words[0] in _NOT_A_NAME:
        words.pop(0)
    return " ".join(words) or ""


def _title(text: str, match: re.Match[str], label: str) -> str:
    """`i have a chemistry exam` -> `chemistry exam`."""
    before = text[: match.start()]
    words = [w for w in re.findall(r"[a-z0-9']+", before) if w not in _NOT_A_NAME]
    if words:
        return _clean_label(f"{words[-1]} {label}") or label
    return label


def _infer_hour(hour: int) -> int:
    """A bare hour with no am/pm: 2 means 2pm, 9 means 9am."""
    if hour > 12:
        return hour
    if hour <= 7:
        return hour + 12
    return hour


def parse_time(text: str) -> tuple[int, int] | None:
    """Clock time in the text, or None."""
    match = _MERIDIEM_RE.search(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if 1 <= hour <= 12:
            if match.group(3) == "p" and hour != 12:
                hour += 12
            elif match.group(3) == "a" and hour == 12:
                hour = 0
            return hour % 24, minute

    match = _COLON_RE.search(text)
    if match:
        return int(match.group(1)) % 24, int(match.group(2))

    match = _AT_RE.search(text)
    if match:
        return _infer_hour(int(match.group(1))) % 24, 0

    for word in ("midnight", "noon", "midday", "lunch", "morning", "afternoon",
                 "evening", "tonight", "night"):
        if re.search(rf"\b{word}\b", text):
            return _NAMED_TIMES[word]
    return None


def parse_day(text: str, now: datetime) -> tuple[Any, bool] | None:
    """(date, may_roll_forward) for the day he said, or None if he said none."""
    if _DAY_AFTER_RE.search(text):
        return (now + timedelta(days=2)).date(), False
    if _TOMORROW_RE.search(text):
        return (now + timedelta(days=1)).date(), False
    if _TODAY_RE.search(text):
        return now.date(), False

    match = _WEEKDAY_RE.search(text)
    if match:
        target = _WEEKDAYS[match.group(1)]
        ahead = (target - now.weekday()) % 7
        return (now + timedelta(days=ahead)).date(), True
    return None


def parse_relative(text: str, now: datetime) -> datetime | None:
    """`in 2 hours` -> now + 2h."""
    match = _RELATIVE_RE.search(text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("h"):
        return now + timedelta(hours=amount)
    return now + timedelta(minutes=amount)


def _negated(text: str, start: int) -> bool:
    window = text[max(0, start - 24):start]
    return _NEGATION_RE.search(window) is not None


def _past_tense(body: str, match: re.Match[str]) -> bool:
    """`i had an exam` and `the test was brutal` are not things to remind."""
    before = body[max(0, match.start() - 30):match.start()]
    if _PAST_RE.search(before):
        return True
    after = body[match.end():match.end() + 30]
    future = _TOMORROW_RE.search(body) or _DAY_AFTER_RE.search(body)
    return bool(_PAST_RE.search(after)) and not future


def choose_lead(cfg: RemindersConfig, rng: random.Random | None = None) -> int:
    """How many minutes before the event she will text him this time."""
    base = max(0, int(cfg.lead_minutes))
    jitter = max(0, int(cfg.lead_jitter_minutes))
    floor = max(0, int(cfg.min_lead_minutes))
    if jitter <= 0:
        return max(floor, base)
    low = max(floor, base - jitter)
    high = max(low, base + jitter)
    pick = rng.uniform(low, high) if rng is not None else random.uniform(low, high)
    return round(pick)


def extract(
    text: str,
    now: datetime,
    cfg: RemindersConfig | None = None,
    rng: random.Random | None = None,
) -> Reminder | None:
    """Pull one upcoming event out of a message, or None."""
    cfg = cfg or RemindersConfig()
    body = _normalise(text)
    if not body:
        return None
    if _YESTERDAY_RE.search(body):
        return None

    found: tuple[re.Match[str], str] | None = None
    for pattern, label in _EVENT_RES:
        match = pattern.search(body)
        if match and not _negated(body, match.start()):
            found = (match, label)
            break
    if found is None:
        return None

    match, label = found
    if _past_tense(body, match):
        return None
    title = _title(body, match, label)
    if not title:
        return None

    when = parse_relative(body, now)
    if when is None:
        clock = parse_time(body)
        day = parse_day(body, now)
        hour, minute = clock if clock else (cfg.default_hour % 24, 0)
        if day is not None:
            target, roll = day
            when = datetime.combine(target, time(hour, minute))
            if roll and when <= now:
                when += timedelta(days=7)
        else:
            when = datetime.combine(now.date(), time(hour, minute))
            if when <= now:
                when += timedelta(days=1)

    lead = choose_lead(cfg, rng)
    moment = when.replace(second=0, microsecond=0)
    return Reminder(
        title=title,
        when=moment.isoformat(timespec="minutes"),
        lead_minutes=lead,
        notify_at=(moment - timedelta(minutes=lead)).isoformat(timespec="minutes"),
        created_at=now.replace(second=0, microsecond=0).isoformat(timespec="minutes"),
    )


# -- storage ----------------------------------------------------------
def load(store: MemoryStore) -> list[Reminder]:
    raw = store.kv_get(KV_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    out: list[Reminder] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            out.append(Reminder(**item))
        except TypeError:
            continue
    return sorted(out, key=lambda r: r.when)


def save(store: MemoryStore, items: Iterable[Reminder]) -> None:
    payload = [asdict(item) for item in items]
    store.kv_set(KV_KEY, json.dumps(payload, ensure_ascii=False))


def upsert(items: Iterable[Reminder], item: Reminder) -> list[Reminder]:
    """Replace a re-stated event instead of reminding him twice."""
    moment = item.at
    kept = [
        old
        for old in items
        if not (
            old.title == item.title
            and old.notified_at is None
            and abs((old.at - moment).total_seconds()) < 6 * 3600
        )
    ]
    kept.append(item)
    kept.sort(key=lambda r: r.when)
    return kept[-MAX_REMINDERS:]


def remember(
    store: MemoryStore,
    text: str,
    now: datetime,
    cfg: RemindersConfig,
    rng: random.Random | None = None,
) -> Reminder | None:
    """Read a message, and store the event if there is one."""
    if not cfg.enabled:
        return None
    item = extract(text, now, cfg, rng)
    if item is None:
        return None
    save(store, upsert(load(store), item))
    return item


def prune(items: Iterable[Reminder], now: datetime) -> list[Reminder]:
    cutoff = now - timedelta(hours=KEEP_PAST_HOURS)
    kept: list[Reminder] = []
    for item in items:
        try:
            moment = item.at
        except ValueError:
            continue
        if moment < cutoff:
            continue
        kept.append(item)
    return kept


def due(items: Iterable[Reminder], now: datetime) -> list[Reminder]:
    """Events whose reminder window is open right now."""
    out: list[Reminder] = []
    for item in items:
        if item.notified_at:
            continue
        try:
            moment = item.at
            start = item.notify
        except ValueError:
            continue
        if start <= now <= moment:
            out.append(item)
    return sorted(out, key=lambda r: r.when)


def pending(items: Iterable[Reminder], now: datetime) -> list[Reminder]:
    out: list[Reminder] = []
    for item in items:
        if item.notified_at:
            continue
        try:
            if item.at >= now:
                out.append(item)
        except ValueError:
            continue
    return sorted(out, key=lambda r: r.when)


# -- wording ----------------------------------------------------------
def format_when(when: datetime, now: datetime) -> str:
    """`at 2pm today`, `at 9am tomorrow`, `on monday at 9am`."""
    clock = _clock(when.hour, when.minute)
    days = (when.date() - now.date()).days
    if days == 0:
        return f"at {clock} today"
    if days == 1:
        return f"at {clock} tomorrow"
    if 1 < days < 7:
        return f"on {when.strftime('%A').lower()} at {clock}"
    return f"on {when.day} {when.strftime('%B').lower()} at {clock}"


def describe(item: Reminder, now: datetime) -> str:
    try:
        return f"your {item.title} is {format_when(item.at, now)}"
    except ValueError:
        return f"your {item.title}"


def describe_many(items: Iterable[Reminder], now: datetime) -> str:
    parts = [describe(item, now) for item in items]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return " and ".join(parts)


def reminder_text(items: Iterable[Reminder], now: datetime) -> str:
    """What she sends if the model gives her nothing usable."""
    body = describe_many(items, now)
    if not body:
        return "hey, dont forget about later"
    return f"hey, {body}. dont forget. good luck"


def upcoming(
    items: Iterable[Reminder], now: datetime, days: float = 2.0
) -> list[str]:
    """Short lines for the system prompt, so she can mention them herself."""
    limit = now + timedelta(days=max(0.0, float(days)))
    lines: list[str] = []
    for item in pending(items, now):
        if item.at > limit:
            break
        lines.append(f"{item.title} {format_when(item.at, now)}")
        if len(lines) >= MAX_PROMPT_LINES:
            break
    return lines
