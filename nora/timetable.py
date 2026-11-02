"""Class timetable: turns timetable.yaml into things she can reason about."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("nora.timetable")

DAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def _parse_time(value: Any) -> time | None:
    if isinstance(value, time):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError:
        return None


@dataclass(frozen=True)
class Slot:
    day: int
    start: time
    end: time
    code: str
    room: str = ""
    tutor: str = ""
    from_week: int | None = None

    def summary(self) -> str:
        """Compact form for the week overview."""
        bits = [f"{self.start:%H:%M}-{self.end:%H:%M}", self.code]
        if self.room:
            bits.append(self.room)
        if self.tutor:
            bits.append(f"({self.tutor})")
        return " ".join(bits)

    def detail(self) -> str:
        bits = [self.code]
        if self.room:
            bits.append(f"in {self.room}")
        if self.tutor:
            bits.append(f"with {self.tutor}")
        return " ".join(bits)


class Timetable:
    def __init__(
        self,
        slots: list[Slot],
        *,
        courses: dict[str, str] | None = None,
        semester: dict[str, Any] | None = None,
    ):
        self.slots = sorted(slots, key=lambda s: (s.day, s.start))
        self.courses = courses or {}
        self.semester = semester or {}

    # -- loading ---------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "Timetable | None":
        path = Path(path)
        if not path.exists():
            log.info("no timetable at %s", path)
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            log.warning("could not read the timetable at %s", path, exc_info=True)
            return None
        return cls.from_dict(data if isinstance(data, dict) else {})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Timetable":
        slots: list[Slot] = []
        for raw in data.get("classes") or []:
            if not isinstance(raw, dict):
                continue
            day_name = str(raw.get("day", "")).strip().lower()
            if day_name not in DAYS:
                log.warning("unknown day in timetable: %r", raw.get("day"))
                continue
            start = _parse_time(raw.get("start"))
            end = _parse_time(raw.get("end"))
            code = str(raw.get("code") or "").strip()
            if not start or not end or not code:
                log.warning("skipping incomplete timetable entry: %r", raw)
                continue
            from_week = raw.get("from_week")
            slots.append(
                Slot(
                    day=DAYS.index(day_name),
                    start=start,
                    end=end,
                    code=code,
                    room=str(raw.get("room") or "").strip(),
                    tutor=str(raw.get("tutor") or "").strip(),
                    from_week=int(from_week) if from_week else None,
                )
            )
        courses = {
            str(k).strip(): str(v).strip()
            for k, v in (data.get("courses") or {}).items()
        }
        semester = data.get("semester") or {}
        return cls(slots, courses=courses, semester=semester if isinstance(semester, dict) else {})

    # -- queries ---------------------------------------------------------
    def week_for(self, moment: datetime | date) -> int | None:
        start = self.semester.get("start_date")
        if not start:
            return None
        if isinstance(start, datetime):
            start_date = start.date()
        elif isinstance(start, date):
            start_date = start
        else:
            try:
                start_date = datetime.strptime(str(start), "%Y-%m-%d").date()
            except ValueError:
                return None
        current = moment.date() if isinstance(moment, datetime) else moment
        return (current - start_date).days // 7 + 1

    def slots_on(self, day: int, week: int | None = None) -> list[Slot]:
        return [
            slot
            for slot in self.slots
            if slot.day == day
            and (week is None or slot.from_week is None or week >= slot.from_week)
        ]

    def current(self, now: datetime) -> Slot | None:
        for slot in self.slots_on(now.weekday(), self.week_for(now)):
            if slot.start <= now.time() < slot.end:
                return slot
        return None

    def next_class(
        self, now: datetime, *, include_today: bool = True
    ) -> tuple[Slot, date] | None:
        for offset in range(0, 8):
            when = now.date() + timedelta(days=offset)
            if offset == 0 and not include_today:
                continue
            for slot in self.slots_on(when.weekday(), self.week_for(when)):
                if offset == 0 and slot.start <= now.time():
                    continue
                return slot, when
        return None

    # -- prose for the prompt --------------------------------------------
    def describe_week(self, week: int | None = None) -> list[str]:
        lines: list[str] = []
        for day in range(7):
            slots = self.slots_on(day, week)
            if not slots:
                continue
            body = "; ".join(slot.summary() for slot in slots)
            lines.append(f"{DAYS[day]}: {body}")
        return lines

    def describe_now(self, now: datetime) -> list[str]:
        today = self.slots_on(now.weekday(), self.week_for(now))
        lines: list[str] = []
        in_class = next(
            (s for s in today if s.start <= now.time() < s.end), None
        )
        upcoming = [s for s in today if s.start > now.time()]

        if not today:
            lines.append("he has no classes today.")
        else:
            if in_class is not None:
                lines.append(
                    f"he is in class right now: {in_class.detail()}, "
                    f"until {in_class.end:%H:%M}."
                )
            if upcoming:
                nxt = upcoming[0]
                lines.append(
                    f"his next class today is {nxt.detail()} at {nxt.start:%H:%M}."
                )
            elif in_class is not None:
                lines.append("that is his last class for today.")
            else:
                lines.append("he is done with classes for today.")

        if not upcoming:
            later = self.next_class(now)
            if later is not None:
                slot, when = later
                lines.append(
                    f"his next class is {DAYS[slot.day]} at {slot.start:%H:%M}, "
                    f"{slot.detail()}."
                )
        return lines
