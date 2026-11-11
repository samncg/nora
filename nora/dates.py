"""Turn "tomorrow" and "november 5th" into real calendar dates.

Two jobs: give the memory clerk's events an absolute date, and rescue older
facts that were written with a relative day so they can expire instead of
being repeated forever.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

_MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# longest first so "sept" is preferred over "sep"
_MONTHS = "|".join(sorted(_MONTH_NAMES, key=len, reverse=True))

_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_DAY_RE = re.compile(
    rf"\b({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?\b"
)
_DAY_MONTH_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTHS})\.?\b(?:,?\s*(\d{{4}}))?"
)
_TOMORROW_RE = re.compile(r"\b(tomorrow|tmr|tmrw|the next day|next day)\b")
_TODAY_RE = re.compile(r"\b(today|tonight|this evening|this afternoon)\b")
# Wording that makes a sentence true only for the next few hours.
_TRANSIENT_RE = re.compile(
    r"\b(currently|right now|at the moment|for now|as we speak|as of today)\b"
)

# Things that only matter on one day. A date plus one of these is an event,
# not a durable fact.
EVENT_WORDS = (
    "exam",
    "exams",
    "final",
    "finals",
    "midterm",
    "test",
    "quiz",
    "viva",
    "interview",
    "meeting",
    "appointment",
    "deadline",
    "assignment",
    "presentation",
    "submission",
    "class",
    "lecture",
    "seminar",
    "lab",
    "flight",
    "train",
    "bus",
    "party",
    "bbq",
    "dinner",
    "lunch",
    "breakfast",
    "trip",
    "concert",
    "match",
    "shift",
    "checkup",
    "dentist",
)

_EVENT_RES = tuple(
    re.compile(rf"\b{re.escape(word)}\b") for word in EVENT_WORDS
)


def _build(year: str | None, month: int, day: int, base: date) -> date | None:
    try:
        if year:
            return date(int(year), month, day)
        guess = date(base.year, month, day)
    except ValueError:
        return None
    # "december" mentioned in january means the december that just passed, and
    # "january" mentioned in december means the one coming up.
    if guess < base - timedelta(days=180):
        try:
            guess = date(base.year + 1, month, day)
        except ValueError:
            return None
    return guess


def parse_date(text: str, *, base: date) -> date | None:
    """The first real calendar date mentioned in the text, or None."""
    body = " ".join((text or "").lower().split())
    if not body:
        return None

    match = _ISO_RE.search(body)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass

    match = _MONTH_DAY_RE.search(body)
    if match:
        month = _MONTH_NAMES[match.group(1)]
        return _build(match.group(3), month, int(match.group(2)), base)

    match = _DAY_MONTH_RE.search(body)
    if match:
        month = _MONTH_NAMES[match.group(2)]
        return _build(match.group(3), month, int(match.group(1)), base)

    if _TOMORROW_RE.search(body):
        return base + timedelta(days=1)
    if _TODAY_RE.search(body):
        return base
    return None


def rewrite_relative(text: str, when: date) -> str:
    """`has an exam tomorrow morning` -> `has an exam on 2026-11-04 morning`."""
    stamp = f"on {when.isoformat()}"
    out = _TOMORROW_RE.sub(stamp, text or "")
    out = _TODAY_RE.sub(stamp, out)
    return " ".join(out.split())


def mentions_event(text: str) -> bool:
    lowered = (text or "").lower()
    return any(pattern.search(lowered) for pattern in _EVENT_RES)


def has_relative_day(text: str) -> bool:
    body = (text or "").lower()
    return bool(_TOMORROW_RE.search(body) or _TODAY_RE.search(body))


def is_transient(text: str) -> bool:
    """`he is currently studying` is a state, not a fact about him."""
    return _TRANSIENT_RE.search((text or "").lower()) is not None


def resolve(text: str, *, base: date) -> tuple[str, date] | None:
    """A dated one-off thing written as a fact, as (rewritten text, date).

    A written date only counts when the sentence is about a one-off thing, so
    "his birthday is on 3 march" stays a permanent fact. Wording like
    "tomorrow" or "tonight" is perishable by definition, whatever it is about.
    """
    if not (mentions_event(text) or has_relative_day(text)):
        return None
    when = parse_date(text, base=base)
    if when is None:
        return None
    return rewrite_relative(text, when), when
