from __future__ import annotations

import base64
import io

from PIL import Image

from nora.vision import MAX_EDGE, prepare_images, to_content_parts


def make_image(size=(16, 16), mode="RGB", color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, "PNG")
    return buffer.getvalue()


def test_prepare_images_returns_a_data_url():
    urls = prepare_images([make_image()])
    assert len(urls) == 1
    assert urls[0].startswith("data:image/jpeg;base64,")


def test_large_images_are_downscaled():
    url = prepare_images([make_image(size=(3000, 2000))])[0]
    raw = base64.b64decode(url.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        assert max(image.size) <= MAX_EDGE


def test_transparent_images_are_flattened_to_jpeg():
    urls = prepare_images([make_image(mode="RGBA", color=(255, 0, 0, 120))])
    assert len(urls) == 1
    raw = base64.b64decode(urls[0].split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        assert image.mode == "RGB"


def test_garbage_is_skipped():
    assert prepare_images([b"definitely not an image"]) == []


def test_at_most_three_images():
    blob = make_image()
    assert len(prepare_images([blob] * 5)) == 3


def test_empty_input():
    assert prepare_images([]) == []


def test_content_parts_shape():
    parts = to_content_parts("look", ["data:image/jpeg;base64,AAA"])
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_content_parts_placeholder_when_no_text():
    parts = to_content_parts("", ["data:image/jpeg;base64,AAA"])
    assert parts[0]["text"] == "look at this"
