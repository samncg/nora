"""Download the Kokoro-82M ONNX model and voice pack for local text-to-speech."""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "models" / "kokoro"
BASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.1"
)
FILES = {
    "kokoro-v1.0.onnx": f"{BASE}/kokoro-v1.0.onnx",
    "voices-v1.0.bin": f"{BASE}/voices-v1.0.bin",
}


def _progress(block: int, block_size: int, total: int) -> None:
    if total <= 0:
        return
    done = min(block * block_size, total)
    if block % 100 == 0:
        print(f"\r  {done * 100 / total:5.1f}%", end="", flush=True)


def download(name: str, url: str) -> None:
    target = DEST / name
    if target.exists() and target.stat().st_size > 0:
        print(f"{name} already exists ({target.stat().st_size / 1e6:.1f} MB)")
        return
    print(f"downloading {name} ...")
    partial = target.with_suffix(target.suffix + ".part")
    try:
        urllib.request.urlretrieve(url, partial, reporthook=_progress)
        print()
        partial.replace(target)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"could not download {name}: {exc}") from exc
    print(f"  saved {target} ({target.stat().st_size / 1e6:.1f} MB)")


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    for name, url in FILES.items():
        download(name, url)
    print(f"Kokoro voice model ready (voice: {settings.tts.voice})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
