from __future__ import annotations

import io

import av
import numpy as np

from nora import config
from nora.tts import Speaker, pcm_48k_stereo, prepare_for_speech


class FakeKokoro:
    def __init__(self, seconds: float = 1.0):
        self.seconds = seconds
        self.calls: list[tuple[str, str, float, str]] = []

    def create(self, text, voice, speed, lang, **kwargs):
        self.calls.append((text, voice, speed, lang))
        count = int(24000 * self.seconds)
        samples = np.sin(np.linspace(0, 80 * np.pi, count)).astype(np.float32) * 0.2
        return samples, 24000


def test_speak_produces_valid_opus_and_waveform():
    settings = config.load()
    settings.tts.enabled = True
    speaker = Speaker(settings)
    speaker._kokoro = FakeKokoro()
    speech = speaker.speak("hello there")
    assert speech is not None
    assert speech.duration > 0.5
    assert len(speech.waveform) == 256
    assert speech.opus[:4] == b"OggS"
    container = av.open(io.BytesIO(speech.opus))
    frames = list(container.decode(audio=0))
    assert frames
    assert speaker._kokoro.calls[0][1] == settings.tts.voice


def test_speak_uses_configured_voice():
    settings = config.load()
    settings.tts.enabled = True
    settings.tts.voice = "af_heart"
    speaker = Speaker(settings)
    fake = FakeKokoro()
    speaker._kokoro = fake
    assert speaker.speak("hey") is not None
    assert fake.calls[0][1] == "af_heart"


def test_prepare_for_speech_expands_shorthand_and_adds_punctuation():
    assert prepare_for_speech("idk tmr tbh") == (
        "i don't know tomorrow to be honest."
    )
    assert prepare_for_speech("im tired but fine") == "i'm tired, but fine."
    ellipsis = prepare_for_speech("hey...you around")
    assert "…" in ellipsis
    assert ellipsis.endswith("around.")
    assert prepare_for_speech("   ") == ""


def test_speak_chunks_synthesizes_each_bubble_with_a_gap():
    settings = config.load()
    settings.tts.enabled = True
    settings.tts.bubble_pause = 0.2
    speaker = Speaker(settings)
    fake = FakeKokoro(seconds=0.5)
    speaker._kokoro = fake
    speech = speaker.speak_chunks(["hey", "you around"])
    assert speech is not None
    assert len(fake.calls) == 2
    # two half-second clips plus the gap between them
    assert speech.duration > 1.15
    speeds = [call[2] for call in fake.calls]
    assert all(0.94 <= speed <= 1.06 for speed in speeds)


def test_speak_is_disabled_by_config():
    settings = config.load()
    settings.tts.enabled = False
    speaker = Speaker(settings)
    speaker._kokoro = FakeKokoro()
    assert speaker.speak("hello") is None


def test_speak_empty_text_returns_nothing():
    settings = config.load()
    settings.tts.enabled = True
    speaker = Speaker(settings)
    speaker._kokoro = FakeKokoro()
    assert speaker.speak("   ") is None


def test_pcm_is_24k_mono_converted_to_48k_stereo_frames():
    samples = np.zeros(24000, dtype=np.float32)
    pcm = pcm_48k_stereo(samples, 24000)
    assert len(pcm) % 3840 == 0
    # 1 second of 48 kHz stereo s16, padded to a 20 ms frame
    assert len(pcm) >= 48000 * 2 * 2
