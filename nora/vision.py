"""Turn Discord attachments into image payloads for the model."""

from __future__ import annotations

import base64
import io
import logging

from PIL import Image, ImageOps

log = logging.getLogger("nora.vision")

MAX_IMAGES = 3
MAX_BYTES = 12 * 1024 * 1024
# Downscale before sending: a phone screenshot stays readable, a 12MP photo
# stops costing thousands of vision tokens.
MAX_EDGE = 1024
JPEG_QUALITY = 85


def prepare_images(blobs: list[bytes]) -> list[str]:
    """Return data URLs, resized and re-encoded, skipping anything unreadable."""
    urls: list[str] = []
    for blob in blobs[:MAX_IMAGES]:
        if not blob or len(blob) > MAX_BYTES:
            continue
        try:
            image = Image.open(io.BytesIO(blob))
            image = ImageOps.exif_transpose(image)
            if image.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", image.size, (255, 255, 255))
                converted = image.convert("RGBA")
                background.paste(converted, mask=converted.split()[-1])
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            image.thumbnail((MAX_EDGE, MAX_EDGE))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        except Exception:
            log.warning("could not read an attached image", exc_info=True)
            continue
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        urls.append(f"data:image/jpeg;base64,{encoded}")
    return urls


def to_content_parts(text: str, urls: list[str]) -> list[dict[str, object]]:
    """OpenAI-style multimodal content for llama.cpp."""
    parts: list[dict[str, object]] = [{"type": "text", "text": text or "look at this"}]
    for url in urls:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts
