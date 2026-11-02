"""Academic calendar: semester breaks, study weeks, exams and holidays."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("nora.academic")

# When ranges overlap, the more specific state wins.
PRIORITY = {"holiday": 0, "break": 1, "exam": 2, "study": 3, "teaching": 4}

VALID_KINDS = set(PRIORITY)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _fmt(day: date | None) -> str:
    if day is None:
        return "further notice"
    return f"{day.day} {day.strftime('%B').lower()}"


@dataclass(frozen=True)
class Event:
    kind: str
    start: date
    end: date
    label: str = ""

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True)
class DayStatus:
    kind: str
    label: str = ""
    until: date | None = None

    @property
    def classes_run(self) -> bool:
        return self.kind == "teaching"


class AcademicCalendar:
    def __init__(
        self,
        events: list[Event],
        *,
        semester: dict[str, Any] | None = None,
    ):
        self.events = sorted(events, key=lambda event: event.start)
        self.semester = semester or {}

    # -- loading ---------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "AcademicCalendar | None":
        path = Path(path)
        if not path.exists():
            log.info("no calendar at %s", path)
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            log.warning("could not read the calendar at %s", path, exc_info=True)
            return None
        return cls.from_dict(data if isinstance(data, dict) else {})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AcademicCalendar":
        events: list[Event] = []
        for raw in data.get("events") or []:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "teaching").strip().lower()
            if kind not in VALID_KINDS:
                log.warning("unknown calendar kind: %r", raw.get("kind"))
                continue
            start = _as_date(raw.get("start") or raw.get("date"))
            end = _as_date(raw.get("end") or raw.get("start") or raw.get("date"))
            if not start or not end:
                log.warning("skipping calendar entry without dates: %r", raw)
                continue
            if end < start:
                start, end = end, start
            events.append(
                Event(kind=kind, start=start, end=end, label=str(raw.get("label") or ""))
            )
        semester = data.get("semester") or {}
        return cls(events, semester=semester if isinstance(semester, dict) else {})

    # -- queries ---------------------------------------------------------
    def status(self, day: date) -> DayStatus:
        hits = [event for event in self.events if event.covers(day)]
        if not hits:
            return DayStatus("teaching")
        best = min(hits, key=lambda event: PRIORITY.get(event.kind, 9))
        return DayStatus(best.kind, best.label, best.end)

    def upcoming(self, day: date, days: int = 10) -> list[tuple[Event, int]]:
        found: list[tuple[Event, int]] = []
        for event in self.events:
            if event.start <= day <= event.end:
                continue
            delta = (event.start - day).days
            if 0 < delta <= days:
                found.append((event, delta))
        found.sort(key=lambda pair: pair[1])
        return found

    # -- prose for the prompt --------------------------------------------
    def describe(self, day: date) -> list[str]:
        status = self.status(day)
        lines: list[str] = []
        if status.kind == "holiday":
            lines.append(f"today is {status.label or 'a public holiday'}, so no classes.")
        elif status.kind == "break":
            lines.append(
                f"he is on semester break until {_fmt(status.until)}. no classes."
            )
        elif status.kind == "study":
            lines.append(
                f"it is study week until {_fmt(status.until)}. no classes, exams are close."
            )
        elif status.kind == "exam":
            lines.append(f"it is his final exam week until {_fmt(status.until)}.")

        for event, delta in self.upcoming(day, 10)[:2]:
            name = event.label or event.kind
            if delta == 1:
                lines.append(f"{name} is tomorrow.")
            else:
                lines.append(f"{name} is in {delta} days ({_fmt(event.start)}).")
        return lines
