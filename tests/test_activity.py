from __future__ import annotations

from nora import activity


class Kind:
    def __init__(self, name: str):
        self.name = name


class Activity:
    def __init__(self, name: str, kind: str = "playing", **extra):
        self.name = name
        self.type = Kind(kind)
        for key, value in extra.items():
            setattr(self, key, value)


class Status:
    def __init__(self, name: str):
        self.name = name


class Member:
    def __init__(self, status: str, activities):
        self.status = Status(status)
        self.activities = activities


def test_playing_a_game():
    line = activity.summary(Status("online"), [Activity("Minecraft")])
    assert line == "he is online and playing Minecraft"


def test_listening_reports_the_artist():
    spotify = Activity("Little Dark Age", kind="listening", artists=["MGMT"])
    assert activity.describe_activity(spotify) == "listening to Little Dark Age by MGMT"


def test_listening_without_an_artist_is_still_fine():
    assert activity.describe_activity(
        Activity("some lofi stream", kind="listening")
    ) == "listening to some lofi stream"


def test_watching_and_competing():
    assert activity.describe_activity(Activity("a stream", kind="watching")) == (
        "watching a stream"
    )
    assert activity.describe_activity(Activity("valorant", kind="competing")) == (
        "competing in valorant"
    )


def test_custom_status_is_quoted_next_to_the_activity():
    custom = Activity("studying", kind="custom", emoji="📚")
    line = activity.summary(Status("online"), [custom, Activity("Minecraft")])
    assert line == 'he is online and playing Minecraft, his status says "📚 studying"'


def test_custom_status_alone():
    custom = Activity("brb food", kind="custom")
    line = activity.summary(Status("online"), [custom])
    assert line == 'he is online, his status says "brb food"'


def test_idle_and_dnd_are_described_in_words():
    assert activity.summary(Status("idle"), []) == "he is idle"
    assert activity.summary(Status("dnd"), []) == "he is on do not disturb"


def test_offline_says_so_and_skips_activities():
    line = activity.summary(Status("offline"), [Activity("Minecraft")])
    assert line == "he is offline right now"


def test_online_with_nothing_showing():
    assert activity.summary(Status("online"), []) == (
        "he is online but nothing is showing"
    )


def test_missing_status_defaults_to_online():
    assert activity.status_word(None) == "online"
    assert activity.status_word("idle") == "idle"


def test_describe_member_reads_the_presence():
    member = Member("online", [Activity("Factorio")])
    assert activity.describe_member(member) == "he is online and playing Factorio"
    assert activity.describe_member(None) is None


def test_unknown_activity_types_do_not_crash():
    weird = Activity("something", kind="unknown", details="in lobby")
    assert activity.describe_activity(weird) == "something (in lobby)"
    assert activity.custom_status([Activity("x", kind="playing")]) == ("", "")
