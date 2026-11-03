"""Local text-to-speech with Kokoro-82M (af_bella), encoded as Ogg/Opus."""

from __future__ import annotations

import io
import logging
import random
import re
import threading
from dataclasses import dataclass

import av
import numpy as np

from .config import Settings

log = logging.getLogger("nora.tts")

# Texting shorthand that Kokoro would otherwise spell out or swallow.
_SPEECH_MAP = {
    "rn": "right now",
    "tmr": "tomorrow",
    "tmrw": "tomorrow",
    "bc": "because",
    "bcs": "because",
    "bcuz": "because",
    "cuz": "because",
    "tho": "though",
    "thru": "through",
    "idk": "i don't know",
    "idc": "i don't care",
    "idfc": "i really don't care",
    "tbh": "to be honest",
    "btw": "by the way",
    "im": "i'm",
    "ive": "i've",
    "youre": "you're",
    "youve": "you've",
    "theyre": "they're",
    "weve": "we've",
    "dont": "don't",
    "doesnt": "doesn't",
    "didnt": "didn't",
    "cant": "can't",
    "couldnt": "couldn't",
    "wouldnt": "wouldn't",
    "shouldnt": "shouldn't",
    "isnt": "isn't",
    "wasnt": "wasn't",
    "werent": "weren't",
    "wont": "won't",
    "havent": "haven't",
    "hasnt": "hasn't",
    "hadnt": "hadn't",
    "aint": "isn't",
    "pls": "please",
    "plz": "please",
    "ty": "thank you",
    "thx": "thanks",
    "smth": "something",
    "somethin": "something",
    "u": "you",
    "ur": "your",
    "r": "are",
    "ok": "okay",
    "omg": "oh my god",
    "fr": "for real",
    "ngl": "not gonna lie",
    "istg": "i swear",
    "wyd": "what are you doing",
    "wya": "where are you",
    "hbu": "how about you",
    "ily": "i love you",
    "imo": "in my opinion",
    "tbf": "to be fair",
    "afaik": "as far as i know",
    "lmk": "let me know",
    "nvm": "never mind",
    "ikr": "i know right",
    "tmi": "too much information",
    "irl": "in real life",
}

_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def prepare_for_speech(text: str) -> str:
    """Turn texting into punctuated prose for the TTS model.

    Kokoro decides sentence and clause pauses from punctuation, so raw
    lowercase chat with no full stops reads flat and rushed.
    """
    text = " ".join(str(text).split())
    if not text:
        return ""
    text = text.replace("&", " and ").replace("%", " percent ")
    text = re.sub(r"\bw/o\b", "without", text, flags=re.IGNORECASE)
    text = re.sub(r"\bw/\b", "with", text, flags=re.IGNORECASE)
    text = re.sub(r"\.{3,}", " … ", text)
    text = _WORD_RE.sub(
        lambda match: _SPEECH_MAP.get(match.group(0).lower(), match.group(0)),
        text,
    )
    # A comma before contrast/consequence clauses gives Kokoro a real pause.
    text = re.sub(
        r"\s+(but|because|though|anyway)\b",
        r", \1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r",\s*,+", ",", text)
    text = re.sub(r"\s+", " ", text).strip().strip(" ,;:")
    if text and text[-1] not in ".!?…":
        text += "."
    return text


@dataclass
class Speech:
    """A synthesized reply, plus what Discord needs to show it as a voice note."""

    samples: np.ndarray
    sample_rate: int
    duration: float
    opus: bytes
    waveform: bytes


class Speaker:
    """Lazy, thread-safe wrapper around the Kokoro-82M ONNX model."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.config = settings.tts
        self._kokoro = None
        self._failed = False
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def _load(self):
        if not self.enabled:
            return None
        if self._kokoro is not None or self._failed:
            return self._kokoro
        with self._load_lock:
            if self._kokoro is not None or self._failed:
                return self._kokoro
            try:
                from kokoro_onnx import Kokoro
            except ImportError:
                log.error(
                    "voice replies need kokoro-onnx; "
                    "run: pip install -r requirements.txt"
                )
                self._failed = True
                return None

            model = self.settings.path(self.config.model)
            voices = self.settings.path(self.config.voices)
            if not model.exists() or not voices.exists():
                log.error(
                    "missing Kokoro files. run: python scripts/fetch_tts.py"
                )
                self._failed = True
                return None
            log.info("loading Kokoro voice model (%s)", self.config.voice)
            try:
                self._kokoro = Kokoro(str(model), str(voices))
            except Exception:
                log.exception("could not load the Kokoro voice model")
                self._failed = True
                return None
        return self._kokoro

    def warmup(self) -> bool:
        return self._load() is not None

    def speak(self, text: str) -> Speech | None:
        """Synthesize one utterance into an Ogg/Opus voice clip."""
        return self.speak_chunks([text])

    def speak_chunks(self, chunks: list[str]) -> Speech | None:
        """Synthesize each text bubble separately, with short gaps between them."""
        parts = [prepare_for_speech(chunk) for chunk in (chunks or [])]
        parts = [part for part in parts if part]
        if not parts or not self.enabled:
            return None
        kokoro = self._load()
        if kokoro is None:
            return None

        base_speed = max(0.5, min(2.0, float(self.config.speed)))
        variation = max(0.0, min(0.25, float(self.config.variation)))
        use_continuous = bool(getattr(kokoro, "has_timings", False)) and bool(
            self.config.continuous
        )
        pieces: list[np.ndarray] = []
        rate: int | None = None

        for index, part in enumerate(parts):
            speed = base_speed * random.uniform(1.0 - variation, 1.0 + variation)
            speed = max(0.5, min(2.0, speed))
            sentence_pause = max(
                0.05,
                float(self.config.sentence_pause) + random.uniform(-0.04, 0.04),
            )
            clause_pause = max(
                0.03,
                float(self.config.clause_pause) + random.uniform(-0.025, 0.025),
            )

            def synth(continuous: bool):
                with self._infer_lock:
                    return kokoro.create(
                        part,
                        voice=self.config.voice,
                        speed=speed,
                        lang=self.config.language,
                        sentence_pause=sentence_pause,
                        clause_pause=clause_pause,
                        continuous=continuous,
                    )

            try:
                samples, sample_rate = synth(use_continuous)
            except Exception:
                log.warning(
                    "continuous TTS failed, retrying without it", exc_info=True
                )
                try:
                    samples, sample_rate = synth(False)
                except Exception:
                    log.exception("Kokoro could not synthesize a reply")
                    return None

            samples = np.asarray(samples, dtype=np.float32).reshape(-1)
            sample_rate = int(sample_rate)
            if samples.size == 0 or sample_rate <= 0:
                return None
            if rate is None:
                rate = sample_rate
            elif sample_rate != rate:
                target = max(1, round(len(samples) * rate / sample_rate))
                source_axis = np.linspace(
                    0.0, 1.0, num=len(samples), endpoint=False
                )
                target_axis = np.linspace(0.0, 1.0, num=target, endpoint=False)
                samples = np.interp(target_axis, source_axis, samples).astype(
                    np.float32
                )

            samples = _fade_edges(samples, sample_rate)
            if index and self.config.bubble_pause > 0:
                pause = float(self.config.bubble_pause) + random.uniform(-0.05, 0.05)
                pieces.append(
                    np.zeros(max(0, int(rate * pause)), dtype=np.float32)
                )
            pieces.append(samples)

        if not pieces or rate is None:
            return None
        samples = np.concatenate(pieces)
        if samples.size == 0:
            return None
        peak = float(np.max(np.abs(samples)))
        if peak > 0:
            samples = samples / peak * 0.95
        try:
            opus = _encode_opus(samples, rate)
        except Exception:
            log.exception("could not encode the synthesized voice")
            return None
        return Speech(
            samples=samples,
            sample_rate=rate,
            duration=len(samples) / float(rate),
            opus=opus,
            waveform=_waveform(samples),
        )


def pcm_48k_stereo(samples: np.ndarray, sample_rate: int) -> bytes:
    """Raw PCM for discord.py's voice client (48 kHz, stereo, s16)."""
    if sample_rate != 48000:
        target = max(1, round(len(samples) * 48000 / sample_rate))
        source = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
        target_axis = np.linspace(0.0, 1.0, num=target, endpoint=False)
        samples = np.interp(target_axis, source, samples).astype(np.float32)
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    data = stereo.tobytes()
    # discord.py feeds 20 ms frames (48k * 2 ch * 2 bytes * 0.02).
    frame_bytes = 3840
    padding = (-len(data)) % frame_bytes
    return data + b"\x00" * padding


def _fade_edges(samples: np.ndarray, sample_rate: int, ms: float = 8.0) -> np.ndarray:
    """Tiny fades stop clicks where one synthesized bubble meets a pause."""
    fade = max(1, int(sample_rate * ms / 1000.0))
    if samples.size > fade * 2:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        samples[:fade] *= ramp
        samples[-fade:] *= ramp[::-1]
    return samples


def _encode_opus(samples: np.ndarray, sample_rate: int) -> bytes:
    frame = av.AudioFrame.from_ndarray(
        samples.reshape(1, -1), format="fltp", layout="mono"
    )
    frame.sample_rate = sample_rate
    buffer = io.BytesIO()
    container = av.open(buffer, mode="w", format="ogg")
    stream = container.add_stream("libopus", rate=48000)
    stream.layout = "mono"
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=48000)
    for converted in resampler.resample(frame):
        for packet in stream.encode(converted):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return buffer.getvalue()


def _waveform(samples: np.ndarray, buckets: int = 256) -> bytes:
    """A 256-byte amplitude sketch, the format Discord uses for voice notes."""
    if samples.size == 0:
        return bytes(buckets)
    chunks = np.array_split(np.abs(samples), buckets)
    peaks = np.array(
        [float(chunk.max()) if chunk.size else 0.0 for chunk in chunks],
        dtype=np.float32,
    )
    peak = float(peaks.max()) or 1.0
    return bytes(
        int(min(255, max(0, round(float(value) / peak * 255))))
        for value in peaks
    )
