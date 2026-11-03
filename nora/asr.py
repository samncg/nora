"""Local speech-to-text for Discord voice messages.

Uses faster-whisper (CTranslate2), so voice notes are transcribed on this
machine. The model is downloaded once into `voice.download_root` and reused.
"""

from __future__ import annotations

import io
import logging
import re
import threading

from .config import Settings

log = logging.getLogger("nora.asr")

# Whisper sometimes emits bracketed stage directions or stray slash/music marks
# when a clip ends in silence. They are not speech and confuse the chat model.
_ARTIFACT_RE = re.compile(
    r"\[(?:music|applause|laughter|silence|blank_audio|inaudible)\]"
    r"|\((?:music|applause|laughter|silence)\)"
    r"|//+|♪+",
    re.IGNORECASE,
)


class Transcriber:
    """Lazy, thread-safe wrapper around a faster-whisper model."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.config = settings.voice
        self._model: object | None = None
        self._failed = False
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def _load(self) -> object | None:
        if not self.enabled:
            return None
        if self._model is not None or self._failed:
            return self._model
        with self._load_lock:
            if self._model is not None or self._failed:
                return self._model
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                log.error(
                    "voice messages need faster-whisper; "
                    "run: pip install -r requirements.txt"
                )
                self._failed = True
                return None

            kwargs: dict[str, object] = {
                "device": self.config.device,
                "compute_type": self.config.compute_type,
            }
            if self.config.download_root:
                root = self.settings.path(self.config.download_root)
                root.mkdir(parents=True, exist_ok=True)
                kwargs["download_root"] = str(root)

            log.info(
                "loading whisper model %s on %s (%s)",
                self.config.model,
                self.config.device,
                self.config.compute_type,
            )
            try:
                self._model = WhisperModel(self.config.model, **kwargs)
            except Exception:
                log.exception("could not load the whisper model")
                self._failed = True
                return None
        return self._model

    def warmup(self) -> bool:
        """Load (and download) the model now instead of on the first voice note."""
        return self._load() is not None

    def transcribe(self, audio: bytes) -> str:
        if not audio or not self.enabled:
            return ""
        model = self._load()
        if model is None:
            return ""

        params = {
            "language": self.config.language or None,
            "beam_size": 5,
            "condition_on_previous_text": False,
        }
        try:
            text = self._run(model, audio, vad_filter=True, **params)
        except Exception:
            # Silero VAD needs onnxruntime; retry without it before giving up.
            log.warning("voice VAD failed, retrying without it", exc_info=True)
            try:
                text = self._run(model, audio, vad_filter=False, **params)
            except Exception:
                log.exception("could not transcribe a voice message")
                return ""

        text = " ".join(text.split())
        text = _ARTIFACT_RE.sub(" ", text)
        text = " ".join(text.split()).strip()
        if text:
            log.info("transcribed voice message: %s", text[:160])
        return text

    def _run(self, model: object, audio: bytes, **kwargs: object) -> str:
        with self._infer_lock:
            segments, _info = model.transcribe(io.BytesIO(audio), **kwargs)  # type: ignore[attr-defined]
            return " ".join(
                segment.text.strip()
                for segment in segments
                if getattr(segment, "text", None) and segment.text.strip()
            )

    def transcribe_many(self, clips: list[bytes]) -> str:
        parts: list[str] = []
        for clip in clips:
            text = self.transcribe(clip)
            if text:
                parts.append(text)
        return "\n".join(parts)
