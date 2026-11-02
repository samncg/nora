from __future__ import annotations

from nora.humanize import (
    estimate_tokens,
    sanitize,
    split_messages,
    split_sentences,
    strip_full_stops,
    typing_seconds,
)


def test_sanitize_strips_ai_formatting():
    raw = "**Sure!** Here you go:\n\n- first thing\n- second thing"
    cleaned = sanitize(raw)
    assert "**" not in cleaned
    assert "- " not in cleaned
    assert "Sure!" in cleaned


def test_sanitize_removes_actions_and_dashes():
    cleaned = sanitize("*sighs* fine — whatever")
    assert "*" not in cleaned
    assert "—" not in cleaned
    assert cleaned.startswith("fine")


def test_sanitize_strips_name_prefix():
    assert sanitize("Nora: hm.") == "hm."


def test_sanitize_collapses_whitespace():
    assert sanitize("hey     there\n\n\nok") == "hey there\nok"


def test_split_messages_respects_limits():
    text = (
        "oh so you survived. are they still being nice to you or was that just "
        "day one. did you actually understand anything or are you googling it at "
        "2am again. anyway. whatever."
    )
    parts = split_messages(text, max_chunks=3, max_chars=120)
    assert 1 <= len(parts) <= 3
    assert all(len(p) <= 160 for p in parts)


def test_split_messages_uses_newlines_as_separate_texts():
    parts = split_messages("hey yeah im here\nwhat's up", max_chunks=3)
    assert parts == ["hey yeah im here", "what's up"]


def test_split_messages_merges_tiny_fragments():
    parts = split_messages("hm.\nok", max_chunks=3)
    assert parts == ["hm ok"]


def test_split_messages_hard_splits_run_on_sentences():
    text = ("word " * 300).strip()  # ~1500 chars, no sentence boundary at all
    parts = split_messages(text, max_chunks=3, max_chars=190)
    assert len(parts) == 3
    assert sum(len(p.split()) for p in parts) == 300


def test_sentences_under_the_hard_limit_stay_whole():
    text = ("word " * 100).strip()  # ~500 chars, still one sentence
    assert split_messages(text, max_chunks=3, max_chars=190) == [text]


def test_merging_joins_whole_sentences():
    parts = split_messages(
        "one two three. four five six. seven eight nine. ten eleven twelve.",
        max_chunks=2,
    )
    assert parts == [
        "one two three four five six",
        "seven eight nine ten eleven twelve",
    ]


def test_never_cuts_a_sentence_in_half():
    text = (
        "finally chilling now\n"
        "ugh my legs are killing me\n"
        "wanna play a game later?\n"
        "ugh i ate too much spicy ramen\n"
        "today my stomach hurts lol\n"
        "you up?"
    )
    parts = split_messages(text, max_chunks=3)

    assert len(parts) == 3
    # every word survives, in order, and no full stops are left behind
    expected = [w.strip(".,!?…") for w in text.replace("\n", " ").split()]
    produced = [w.strip(".,!?…") for w in " ".join(parts).split()]
    assert produced == expected
    assert not any(part.endswith(".") for part in parts)
    # the bug this replaces produced "...i ate too much" / "spicy ramen ..."
    assert not any(part.endswith("too much") for part in parts)


def test_split_messages_empty():
    assert split_messages("   \n  ") == []


def test_split_sentences():
    assert split_sentences("one. two! three?") == ["one.", "two!", "three?"]


def test_emoji_are_removed_completely():
    assert sanitize("ugh that sucks 😒") == "ugh that sucks"
    assert sanitize("lol 😂😂 whatever") == "lol whatever"
    assert sanitize("so tired 🥱") == "so tired"
    assert sanitize("🏀 lets go") == "lets go"
    # an emoji-only reply leaves nothing behind
    assert sanitize("💤") == ""


def test_emoji_removal_keeps_normal_punctuation():
    assert sanitize("wait... really?") == "wait... really?"
    assert sanitize("47/100") == "47/100"


def test_common_words_are_shortened():
    assert sanitize("i dont know what to do right now") == "idk what to do rn"
    assert sanitize("i don't know what to do right now") == "idk what to do rn"
    assert sanitize("see you tomorrow") == "see you tmr"
    assert sanitize("see you tommorow lol") == "see you tmr lol"
    assert sanitize("im tired because of work though") == "im tired bc of work tho"
    assert sanitize("probably gonna stay in") == "prob gonna stay in"
    assert sanitize("by the way im sorry") == "btw im sorry"
    assert sanitize("do you want to come over") == "do you wanna come over"
    assert sanitize("i need something at the moment") == "i need smth atm"


def test_shorthand_never_touches_words_by_accident():
    assert sanitize("somewhere over there") == "somewhere over there"
    assert sanitize("i got to the store") == "i got to the store"
    assert sanitize("people watching people") == "ppl watching ppl"


def test_shorthand_does_not_join_lines():
    # a phrase split across two texts must not be collapsed into one
    assert sanitize("right\nnow") == "right\nnow"


def test_sanitize_repairs_missing_spaces_after_punctuation():
    assert sanitize("rough.dont") == "rough. dont"
    assert sanitize("wait...but") == "wait... but"
    assert sanitize("3.5 hours") == "3.5 hours"
    assert sanitize("e.g. this") == "e.g. this"


def test_sanitize_keeps_commas():
    assert sanitize("yeah, i guess") == "yeah, i guess"
    assert sanitize("it costs 1,000 dollars") == "it costs 1,000 dollars"


def test_dashes_do_not_leave_stray_punctuation():
    assert sanitize("fine — whatever") == "fine, whatever"
    assert sanitize("okay. — anyway") == "ok. anyway"


def test_full_stops_are_removed_from_outgoing_texts():
    assert split_messages("yeah i guess. sure why not.") == [
        "yeah i guess",
        "sure why not",
    ]
    assert strip_full_stops("ugh that sucks.") == "ugh that sucks"


def test_ellipses_question_marks_and_decimals_survive():
    assert strip_full_stops("idk... whatever") == "idk... whatever"
    assert strip_full_stops("you ok?") == "you ok?"
    assert strip_full_stops("3.5 hours") == "3.5 hours"
    # trailing off is a natural place to break into two texts
    assert split_messages("wait... really?") == ["wait...", "really?"]
    assert split_messages("idk... whatever") == ["idk...", "whatever"]


def test_run_on_line_is_broken_into_thoughts():
    parts = split_messages(
        "nice poke bowl sounds pretty good honestly text me when u get back ok... "
        "dont stay out too late tho"
    )
    assert parts == [
        "nice poke bowl sounds pretty good",
        "honestly text me when u get back ok...",
        "dont stay out too late tho",
    ]


def test_affectionate_run_on_becomes_two_bubbles():
    assert split_messages("of course i do dummy i miss you already") == [
        "of course i do dummy",
        "i miss you already",
    ]


def test_soft_split_leaves_short_clauses_alone():
    # splitting before "i was" here would break the sentence
    assert split_messages("i told you i was tired yesterday") == [
        "i told you i was tired yesterday"
    ]


def test_soft_split_does_not_make_tiny_fragments():
    for part in split_messages("i miss you so much i really do"):
        assert len(part) >= 8


def test_run_together_sentences_become_separate_texts():
    text = (
        "man thats rough.dont stress too much though."
        "we can look at notes later if u want...but seriously get some sleep first."
    )
    parts = split_messages(text, max_chunks=3)

    assert parts == [
        "man thats rough dont stress too much tho",
        "we can look at notes later if u want...",
        "but seriously get some sleep first",
    ]


def test_abbreviations_are_not_sentence_ends():
    assert split_sentences("e.g. this is fine. and this too.") == [
        "e.g. this is fine.",
        "and this too.",
    ]


def test_typing_seconds_bounds():
    fast = typing_seconds("hi", cps=50, minimum=0.2, maximum=1.0)
    slow = typing_seconds("x" * 500, cps=2, minimum=0.2, maximum=4.0)
    assert 0.2 <= fast <= 1.0
    assert slow == 4.0


def test_estimate_tokens_grows():
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)
