"""What he is doing on discord, in one line she can actually use.

Duck-typed on purpose: the bot hands us a discord.Member, tests hand us small
stubs, and neither shape is worth a hard dependency here.
"""

from __future__ import annotations

from typing import Any, Iterable

STATUS_WORDS = {
    "online": "online",
    "idle": "idle",
    "dnd": "on do not disturb",
    "offline": "offline",
    "invisible": "offline",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _kind(activity: Any) -> str:
    raw = getattr(activity, "type", None)
    name = getattr(raw, "name", None)
    if name:
        return str(name).lower()
    return str(raw or "").lower()


def status_word(status: Any) -> str:
    key = getattr(status, "name", status)
    return STATUS_WORDS.get(str(key or "").lower(), "online")


def describe_activity(activity: Any) -> str:
    """`playing minecraft`, `listening to x by y`, or "" for a custom status."""
    kind = _kind(activity)
    if kind == "custom":
        return ""
    title = _text(getattr(activity, "name", ""))
    if kind == "listening":
        artists = [a for a in (getattr(activity, "artists", None) or []) if a]
        if title and artists:
            return f"listening to {title} by {', '.join(_text(a) for a in artists[:2])}"
        return f"listening to {title}" if title else "listening to something"
    if kind == "watching":
        return f"watching {title}" if title else ""
    if kind == "playing":
        return f"playing {title}" if title else ""
    if kind == "competing":
        return f"competing in {title}" if title else ""
    detail = _text(getattr(activity, "details", "")) or _text(
        getattr(activity, "state", "")
    )
    if title and detail:
        return f"{title} ({detail})"
    return title


def custom_status(activities: Iterable[Any] | None) -> tuple[str, str]:
    """(text, emoji) of his custom status, if he has one."""
    for activity in activities or []:
        if _kind(activity) != "custom":
            continue
        text = _text(getattr(activity, "name", "")) or _text(
            getattr(activity, "state", "")
        )
        emoji = _text(getattr(activity, "emoji", None))
        return text, emoji
    return "", ""


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def summary(status: Any, activities: Iterable[Any] | None) -> str | None:
    """One sentence for the prompt, or None when there is nothing to say."""
    words = status_word(status)
    if words == "offline":
        return "he is offline right now"

    doing = [line for line in (describe_activity(a) for a in activities or []) if line]
    text, emoji = custom_status(activities)
    label = _text(f"{emoji} {text}".strip())

    line = f"he is {words}"
    if doing:
        line += " and " + _join(doing)
    if label:
        line += f', his status says "{label}"'
    if not doing and not label and words == "online":
        line += " but nothing is showing"
    return line


def describe_member(member: Any) -> str | None:
    if member is None:
        return None
    return summary(
        getattr(member, "status", None), getattr(member, "activities", None)
    )
