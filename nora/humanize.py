"""Make model output look like a human texting on a phone."""

from __future__ import annotations

import random
import re
import unicodedata

_FENCE_RE = re.compile(r"```.*?```", re.S)
_ACTION_RE = re.compile(r"\*[^*\n]{1,60}\*")
_BOLD_RE = re.compile(r"(\*\*|__)(.*?)\1")
_CODE_RE = re.compile(r"`([^`]*)`")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET_RE = re.compile(r"^\s*[-•]\s+", re.M)
_LEAD_NAME_RE = re.compile(r"^\s*(?:nora|assistant|you)\s*:\s*", re.I)
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")

# She never uses emoji, and models add them anyway.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # pictographs, symbols, faces, flags
    "\U00002600-\U000027BF"  # misc symbols and dingbats
    "\U00002B00-\U00002BFF"  # arrows and stars
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0001F1E6-\U0001F1FF"  # regional indicators
    "\U0000200D"             # zero width joiner
    "\U000024C2\U00003030\U0000303D\U00002049"
    "]+"
)
# A sentence ends on . ! ? — an ellipsis is a pause, so it is protected first.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_ELLIPSIS_TOKEN = "\u0001"

# Sentences this long are the only ones ever broken up by words.
HARD_SPLIT_CHARS = 600

_SINGLE_LETTER_TAIL_RE = re.compile(r"(?:^|[\s(\"'])[A-Za-z]$")
_ABBREV_TAIL_RE = re.compile(
    r"(?:\be\.g\.?|\bi\.e\.?|\betc\.?|\bmr\.?|\bmrs\.?|\bms\.?|\bdr\.?|\bvs\.?|\bst\.?)$",
    re.I,
)


def _replace_dashes(text: str) -> str:
    """A dash is a pause, so it becomes a comma when one is needed."""

    def repl(match: re.Match) -> str:
        before = text[: match.start()].rstrip()
        if not before or before[-1] in ".!?…,":
            return " "
        return ", "

    return re.sub(r"\s*[—–]+\s*", repl, text)


def strip_full_stops(text: str) -> str:
    """Texting style: no sentence-ending full stops.

    Ellipses and decimals survive, and question/exclamation marks are kept.
    This runs per outgoing text, after sentence splitting has used the stops.
    """
    if not text:
        return text
    protected = re.sub(r"\.{2,}", "\u2026", text)
    protected = re.sub(r"\.(?=\s|$)", "", protected)
    protected = protected.replace("\u2026", "...")
    protected = re.sub(r"[ \t]{2,}", " ", protected)
    return protected.strip()


def _space_after_punctuation(text: str) -> str:
    """Repair missing spaces like `rough.dont` before sentence splitting.

    Small models drop the space after a full stop while texting, which used to
    make the whole reply look like a single sentence (and therefore a single
    very long message). Decimals and abbreviations are left alone.
    """
    if not text:
        return text
    out: list[str] = []
    for index, char in enumerate(text):
        out.append(char)
        if char not in ".!?…":
            continue
        following = text[index + 1 : index + 2]
        if not following.isalpha():
            continue
        before = text[:index]
        if _SINGLE_LETTER_TAIL_RE.search(before):
            continue  # e.g. / i.e. / initials
        if _ABBREV_TAIL_RE.search(before):
            continue
        out.append(" ")
    return "".join(out)


# Texting shorthand. Applied after generation, because a small model will
# never be consistent about it on its own.
_SHORTEN_RULES = [
    (re.compile(r"\bwhat[ \t]+are[ \t]+you[ \t]+doing\b"), "wyd"),
    (re.compile(r"\bi[ \t]+do(?:n'?t|nt)[ \t]+know\b"), "idk"),
    (re.compile(r"\bi[ \t]+do(?:n'?t|nt)[ \t]+care\b"), "idc"),
    (re.compile(r"\bas[ \t]+soon[ \t]+as[ \t]+possible\b"), "asap"),
    (re.compile(r"\bto[ \t]+be[ \t]+honest\b"), "tbh"),
    (re.compile(r"\bin[ \t]+my[ \t]+opinion\b"), "imo"),
    (re.compile(r"\bby[ \t]+the[ \t]+way\b"), "btw"),
    (re.compile(r"\bat[ \t]+the[ \t]+moment\b"), "atm"),
    (re.compile(r"\bfor[ \t]+real\b"), "fr"),
    (re.compile(r"\bnever[ \t]+mind\b"), "nvm"),
    (re.compile(r"\bthank[ \t]+you\b"), "ty"),
    (re.compile(r"\bright[ \t]+now\b"), "rn"),
    (re.compile(r"\bgoing[ \t]+to\b"), "gonna"),
    (re.compile(r"\bwant[ \t]+to\b"), "wanna"),
    (re.compile(r"\bkind[ \t]+of\b"), "kinda"),
    (re.compile(r"\bsort[ \t]+of\b"), "sorta"),
    # covers tomorrow / tommorow / tomorow
    (re.compile(r"\btom+or+ow\b"), "tmr"),
    (re.compile(r"\bbecause\b"), "bc"),
    (re.compile(r"\bthough\b"), "tho"),
    (re.compile(r"\bprobably\b"), "prob"),
    (re.compile(r"\bdefinitely\b"), "def"),
    (re.compile(r"\bobviously\b"), "obv"),
    (re.compile(r"\bsomething\b"), "smth"),
    (re.compile(r"\bpeople\b"), "ppl"),
    (re.compile(r"\bplease\b"), "pls"),
    (re.compile(r"\bokay\b"), "ok"),
]


def _shorten(text: str) -> str:
    """Turn the words people always shorten into their texting form."""
    for pattern, replacement in _SHORTEN_RULES:
        text = pattern.sub(replacement, text)
    return text


def sanitize(text: str) -> str:
    """Strip formatting that instantly reads as 'AI' in a text conversation."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _EMOJI_RE.sub("", text)
    text = _FENCE_RE.sub("", text)
    text = _CODE_RE.sub(r"\1", text)
    text = _BOLD_RE.sub(r"\2", text)
    text = _ACTION_RE.sub("", text)
    text = _HEADING_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _LEAD_NAME_RE.sub("", text)
    text = _replace_dashes(text)
    text = _space_after_punctuation(text)
    text = text.replace("**", "").replace("*", "")
    text = text.replace("“", '"').replace("”", '"').replace("’", "'")
    text = _shorten(text)
    text = _MULTISPACE_RE.sub(" ", text)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    text = text.strip().strip('"').strip()
    return text


def split_sentences(text: str) -> list[str]:
    parts = [
        piece.strip()
        for piece in _SENTENCE_SPLIT_RE.split(text)
        if piece.strip()
    ]
    merged: list[str] = []
    for part in parts:
        # "e.g." is not the end of a sentence, keep it with what follows
        if merged and _ABBREV_TAIL_RE.search(merged[-1]):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


# Words that usually start a new thought in a text bubble.
_SOFT_BREAK_MIN_UNIT = 30
_SOFT_BREAK_MIN_LEFT = 14
_SOFT_BREAK_MIN_RIGHT = 14

_SOFT_BREAK_RE = re.compile(
    r"[ \t]+(?=(?:"
    r"i (?:miss|love|hope|think|feel|want|need|really|cant|can't|dont|don't|"
    r"didnt|didn't|should|would|could|will|had|was|am|knew)"
    r"|im|i'm|ill|i'll|ive|i've|idk|idc"
    r"|text me|let me know|call me|come over|see you"
    r"|anyway|btw|also|then|honestly|tho|though|you should|we should"
    r")\b)",
    re.I,
)


def _soft_split(unit: str) -> list[str]:
    """Break a run-on bubble before a word that starts a new thought.

    Models happily emit "of course i do dummy i miss you already" as one
    line. A person would send that as two texts.
    """
    if len(unit) <= _SOFT_BREAK_MIN_UNIT:
        return [unit]

    pieces: list[str] = []
    start = 0
    for match in _SOFT_BREAK_RE.finditer(unit):
        cut = match.start()
        if cut - start < _SOFT_BREAK_MIN_LEFT:
            continue
        if len(unit) - cut < _SOFT_BREAK_MIN_RIGHT:
            break
        pieces.append(unit[start:cut].strip())
        start = cut

    tail = unit[start:].strip()
    if tail:
        pieces.append(tail)
    return [piece for piece in pieces if piece]


def _hard_split(text: str, max_chars: int) -> list[str]:
    words = text.split()
    out: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                out.append(current)
            current = word
    if current:
        out.append(current)
    return out


def _units(text: str, max_chars: int) -> list[str]:
    """Sentences are indivisible: she never cuts through one mid-thought.

    Only a genuinely run-on sentence (longer than HARD_SPLIT_CHARS) gets
    word-split, because at that point there is no sentence edge to use.
    """
    units: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for sentence in split_sentences(paragraph):
            if len(sentence) > HARD_SPLIT_CHARS:
                units.extend(_hard_split(sentence, max_chars))
            else:
                units.append(sentence)
    return units


def _merge_tiny(units: list[str], max_chars: int) -> list[str]:
    """Glue tiny standalone fragments ("hm.", "ok") onto the previous text."""
    merged: list[str] = []
    for unit in units:
        if merged and len(unit) <= 6 and len(merged[-1]) + len(unit) + 1 <= max_chars:
            merged[-1] = f"{merged[-1]} {unit}"
        else:
            merged.append(unit)
    return merged


def _merge_closest_pair(units: list[str]) -> list[str]:
    """Join the two neighbouring sentences that lose the least by merging.

    Keeping whole sentences matters more than hitting an exact message
    length, so when there are too many texts we fold them instead of
    re-slicing the string by character count.
    """
    if len(units) < 2:
        return units
    index = min(
        range(len(units) - 1), key=lambda i: len(units[i]) + len(units[i + 1])
    )
    joined = f"{units[index]} {units[index + 1]}"
    return units[:index] + [joined] + units[index + 2 :]


def split_messages(
    text: str, *, max_chunks: int = 3, max_chars: int = 190
) -> list[str]:
    """Turn one model reply into the 1-3 short texts a person would send."""
    text = sanitize(text)
    if not text:
        return []

    max_chunks = max(1, max_chunks)
    units: list[str] = []
    for unit in _units(text, max_chars):
        units.extend(_soft_split(unit))
    units = _merge_tiny(units, max_chars)
    while len(units) > max_chunks:
        units = _merge_closest_pair(units)

    cleaned: list[str] = []
    for unit in units:
        unit = strip_full_stops(unit)
        if unit:
            cleaned.append(unit)
    return cleaned


def typing_seconds(
    text: str, *, cps: float = 9.0, minimum: float = 0.6, maximum: float = 6.0
) -> float:
    """How long a person would plausibly take to type this message."""
    base = len(text) / max(cps, 0.5)
    jitter = random.uniform(0.0, 0.45)
    return max(minimum, min(maximum, base + jitter))


def estimate_tokens(text: str) -> int:
    """Cheap token estimate; good enough for context budgeting."""
    return max(1, len(text) // 4)
