from __future__ import annotations

from types import SimpleNamespace

from nora import config
from nora.asr import Transcriber


class FakeModel:
    def __init__(self, texts: list[str], *, fail: bool = False):
        self.texts = texts
        self.fail = fail
        self.calls = 0

    def transcribe(self, audio, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        segments = [SimpleNamespace(text=t) for t in self.texts]
        return segments, SimpleNamespace(duration=1.2)


def transcriber_with(texts: list[str], *, fail: bool = False) -> Transcriber:
    settings = config.load()
    t = Transcriber(settings)
    t._model = FakeModel(texts, fail=fail)
    return t


def test_transcribe_joins_segments_and_normalizes_space():
    t = transcriber_with([" hello", "there ", "friend"])
    assert t.transcribe(b"fake") == "hello there friend"


def test_transcribe_empty_audio_returns_empty():
    t = transcriber_with(["hello"])
    assert t.transcribe(b"") == ""


def test_transcribe_is_disabled_by_config():
    settings = config.load()
    settings.voice.enabled = False
    t = Transcriber(settings)
    t._model = FakeModel(["hello"])
    assert t.transcribe(b"fake") == ""
    assert t.warmup() is False


def test_transcribe_many_joins_clips():
    t = transcriber_with(["first"])
    assert t.transcribe_many([b"a", b"b"]) == "first\nfirst"


def test_transcribe_strips_non_speech_artifacts():
    t = transcriber_with([" hello there ", "//", "♪"])
    assert t.transcribe(b"fake") == "hello there"


def test_transcription_failure_returns_empty():
    t = transcriber_with([], fail=True)
    assert t.transcribe(b"fake") == ""
