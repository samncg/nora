"""Pre-download the local Whisper model used for voice messages."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nora import config
from nora.asr import Transcriber


def main() -> int:
    settings = config.load()
    # The script is also useful when the feature is off in config.yaml.
    settings.voice.enabled = True
    transcriber = Transcriber(settings)
    if transcriber.warmup():
        print(f"voice model ready: {settings.voice.model}")
        return 0
    print(
        "could not load the voice model. make sure faster-whisper is installed "
        "and this machine can reach huggingface.co once.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
