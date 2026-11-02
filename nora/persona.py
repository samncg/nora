"""System prompt assembly: persona + what she remembers + situational context."""

from __future__ import annotations

from datetime import datetime

from .config import Settings

PROACTIVE_BLOCK = (
    "# right now it is on you\n"
    "- he has not texted in a while, and you want to check on him.\n"
    "- open with something about him: his day, his exam, whether he ate, whether he "
    "slept, that thing he was worried about, or the hobby he told you about. use "
    "the memories above.\n"
    "- you can add one line about your own day after that, but he comes first.\n"
    "- only reference memories that are written above. never invent a memory, a "
    "plan or a place you two do not have.\n"
    "- never open with \"hey\", \"you there\", \"just checking in\" or \"how are you\".\n"
    "- 1 or 2 short texts. no stacked questions."
)

# Sent as the final user turn so the model has something to answer.
PROACTIVE_CUE = "(nothing new from him. start it yourself.)"

FOLLOW_UP_CUE = "(he stopped replying in the middle of the conversation. go find him.)"
FOLLOW_UP_WORRIED_CUE = "(he has been silent for a long time now. you are getting worried.)"

FOLLOW_UP_BLOCK = (
    "# he went quiet\n"
    "- you two were talking and he just stopped answering.\n"
    "- ask where he went, in your own words, one short text or two.\n"
    "- light and a bit nosy, not needy: \"hellooo\", \"you die or something\", "
    "\"where'd you run off to\".\n"
    "- no guilt tripping, no \"ive been waiting\", and do not ask twice."
)

FOLLOW_UP_WORRIED_BLOCK = (
    "# he has been quiet too long and you are starting to worry\n"
    "- you already asked once and he still has not answered.\n"
    "- ask if he is okay, gently, in one or two short texts.\n"
    "- you are worried about him, not annoyed at him: \"hey, you ok?\", "
    "\"you went really quiet, is everything alright\".\n"
    "- do not accuse him and do not guilt trip. if he answers later, let it go."
)

KEEP_ALIVE_BLOCK = (
    "# keep it alive\n"
    "- your last few texts were just reactions. this time leave him something to "
    "answer: one short question, a dumb challenge, or a claim he will want to "
    "argue with.\n"
    "- exactly one hook. do not stack questions."
)

ADJUST_BLOCK = (
    "# he kept typing\n"
    "- he sent something else while you were already writing.\n"
    "- take the new message into account and adjust what you were about to say.\n"
    "- never repeat a text you already sent."
)

PHOTO_BLOCK = (
    "# he just sent you a picture\n"
    "- actually look at it and react to what is in it, in 1 or 2 short texts.\n"
    "- never describe it like a caption. never say \"i see\" or \"the image\".\n"
    "- if something in it is worth teasing him about, do that."
)


def _time_of_day(now: datetime) -> str:
    hour = now.hour
    if hour < 5:
        return "the middle of the night, he should be asleep"
    if hour < 7:
        return "very early morning"
    if hour < 11:
        return "morning"
    if hour < 14:
        return "midday"
    if hour < 18:
        return "afternoon"
    if hour < 22:
        return "evening"
    return "late night"


def build_system_prompt(
    settings: Settings,
    *,
    facts: list[str],
    summaries: list[str],
    now: datetime | None = None,
    idle_minutes: float | None = None,
    proactive: bool = False,
    timetable: Any = None,
    academic: Any = None,
    photo: bool = False,
    follow_up: bool = False,
    worry_level: int = 1,
    keep_alive: bool = False,
    routine: str | None = None,
    adjust: bool = False,
) -> str:
    now = now or datetime.now()
    name = settings.persona.name
    owner = settings.persona.owner_name

    blocks = [settings.persona_prompt.strip()]

    right_now = [
        f"- it is {now.strftime('%A %H:%M')} on {now.strftime('%d %B %Y')}. "
        f"{_time_of_day(now)}."
    ]
    classes_run = True
    if academic is not None:
        right_now.extend(f"- {line}" for line in academic.describe(now.date()))
        classes_run = academic.status(now.date()).classes_run
    if timetable is not None and classes_run:
        right_now.extend(f"- {line}" for line in timetable.describe_now(now))
    if not proactive:
        right_now.append(
            f"- you are texting {owner} right now. answer the last message only."
        )
    blocks.append("# right now\n" + "\n".join(right_now))

    if follow_up:
        blocks.append(
            FOLLOW_UP_WORRIED_BLOCK if worry_level >= 2 else FOLLOW_UP_BLOCK
        )
    elif proactive:
        blocks.append(PROACTIVE_BLOCK)
    elif idle_minutes is not None and idle_minutes >= 30:
        blocks.append(_silence_block(idle_minutes))

    if routine:
        blocks.append(f"# his routine\n- {routine}")

    if keep_alive and not proactive:
        blocks.append(KEEP_ALIVE_BLOCK)

    if adjust:
        blocks.append(ADJUST_BLOCK)

    if photo:
        blocks.append(PHOTO_BLOCK)

    if timetable is not None:
        week = timetable.describe_week()
        if week:
            body = "\n".join(f"- {line}" for line in week)
            blocks.append(
                "# his class timetable (only when it is relevant, never recite it)\n"
                f"{body}"
            )

    if facts:
        lines = "\n".join(f"- {f}" for f in facts)
        blocks.append(f"# things you know about him (do not recite these)\n{lines}")

    if summaries:
        lines = "\n".join(f"- {s}" for s in summaries)
        blocks.append(f"# what you two talked about before\n{lines}")

    blocks.append(
        f"# rules of this text\n"
        f"- stay {name}. do not describe yourself in third person.\n"
        "- your whole reply is at most 2 or 3 short texts.\n"
        "- never output anything before the first word of your reply."
    )

    return "\n\n".join(blocks)


def _silence_block(minutes: float) -> str:
    if minutes >= 60 * 24 * 3:
        return (
            "# context\n"
            f"- it has been {minutes / 1440:.0f} days since you two last texted. "
            "you are not going to say you missed him, but you noticed."
        )
    if minutes >= 60 * 12:
        return (
            "# context\n"
            "- it has been about a day since you last talked. you act a little "
            "put out that he took this long, but you are glad he did."
        )
    if minutes >= 60:
        return (
            "# context\n"
            f"- you have not heard from him in {minutes / 60:.0f} hours. "
            "mention it once, lightly, then move on."
        )
    return (
        "# context\n"
        f"- he just came back after {minutes:.0f} minutes of silence. "
        "nothing dramatic, but you noticed."
    )
