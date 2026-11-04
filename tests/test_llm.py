from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from nora.config import Settings
from nora.llm import LlamaClient, _free_model_ids, _openrouter_error_text


def make_settings() -> Settings:
    settings = Settings()
    settings.provider.mode = "openrouter"
    settings.provider.openrouter.api_key = "sk-or-test"
    settings.provider.openrouter.model = "google/gemma-4-31b-it:free"
    return settings


def test_openrouter_payload_uses_standard_fields_only():
    client = LlamaClient(make_settings())
    payload = client.openrouter_payload([{"role": "user", "content": "hi"}])

    assert payload["model"] == "google/gemma-4-31b-it:free"
    assert payload["stream"] is False
    assert payload["reasoning"] == {"enabled": False}
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    # llama.cpp-only knobs would be ignored or rejected by many providers
    for llama_only in ("cache_prompt", "chat_template_kwargs", "top_k", "repeat_penalty"):
        assert llama_only not in payload


def test_openrouter_headers_carry_the_key():
    client = LlamaClient(make_settings())
    headers = client._openrouter_headers()

    assert headers["Authorization"] == "Bearer sk-or-test"
    assert headers["HTTP-Referer"]
    assert headers["X-Title"] == "Nora"


def test_openrouter_requires_a_key():
    settings = make_settings()
    settings.provider.openrouter.api_key = None

    with pytest.raises(RuntimeError, match="no API key"):
        LlamaClient(settings)._openrouter_headers()


def test_openrouter_requires_a_model():
    settings = make_settings()
    settings.provider.openrouter.model = ""

    with pytest.raises(RuntimeError, match="no model"):
        LlamaClient(settings).openrouter_payload([])


def test_chat_routes_to_openrouter(monkeypatch):
    client = LlamaClient(make_settings())
    seen: dict[str, object] = {}

    async def fake_chat(messages, **overrides):
        seen["messages"] = messages
        seen["overrides"] = overrides
        return "hey"

    monkeypatch.setattr(client, "_openrouter_chat", fake_chat)
    result = asyncio.run(client.chat([{"role": "user", "content": "hi"}], temperature=0.5))

    assert result == "hey"
    assert seen["messages"] == [{"role": "user", "content": "hi"}]
    assert seen["overrides"] == {"temperature": 0.5}


def test_local_provider_does_not_call_openrouter(monkeypatch):
    settings = Settings()
    assert settings.provider.mode == "local"
    client = LlamaClient(settings)

    async def fail_openrouter(*args, **kwargs):
        raise AssertionError("openrouter should not be used in local mode")

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "hi there"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    async def noop(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(client, "_openrouter_chat", fail_openrouter)
    monkeypatch.setattr(client.server, "ensure_running", noop)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert result == "hi there"


def test_openrouter_falls_back_to_local(monkeypatch):
    client = LlamaClient(make_settings())

    async def fail_openrouter(*args, **kwargs):
        raise RuntimeError("OpenRouter rate-limited the request (429).")

    async def local_answer(*args, **kwargs):
        return "local answer"

    monkeypatch.setattr(client, "_openrouter_chat", fail_openrouter)
    monkeypatch.setattr(client, "_local_chat", local_answer)

    result = asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert result == "local answer"


def test_openrouter_cools_down_after_a_429(monkeypatch):
    client = LlamaClient(make_settings())
    calls = {"openrouter": 0, "local": 0}

    async def fail_openrouter(*args, **kwargs):
        calls["openrouter"] += 1
        raise RuntimeError("OpenRouter rate-limited the request (429).")

    async def local_answer(*args, **kwargs):
        calls["local"] += 1
        return "local answer"

    monkeypatch.setattr(client, "_openrouter_chat", fail_openrouter)
    monkeypatch.setattr(client, "_local_chat", local_answer)

    messages = [{"role": "user", "content": "hi"}]
    asyncio.run(client.chat(messages))
    asyncio.run(client.chat(messages))

    # the second call skips OpenRouter entirely during the cooldown
    assert calls == {"openrouter": 1, "local": 2}


def test_openrouter_fallback_can_be_disabled(monkeypatch):
    settings = make_settings()
    settings.provider.fallback_to_local = False
    client = LlamaClient(settings)

    async def fail_openrouter(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "_openrouter_chat", fail_openrouter)

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))


def test_openrouter_error_text_includes_the_upstream_hint():
    resp = SimpleNamespace(
        status_code=429,
        json=lambda: {
            "error": {
                "message": "Provider returned error",
                "metadata": {"raw": "temporarily rate-limited upstream"},
            }
        },
    )

    text = _openrouter_error_text(resp)

    assert "429" in text
    assert "temporarily rate-limited upstream" in text


def test_openrouter_200_error_body_is_a_failure(monkeypatch):
    settings = make_settings()
    settings.provider.fallback_to_local = False
    client = LlamaClient(settings)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"error": {"message": "temporarily rate-limited upstream"}}

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(RuntimeError, match="rate-limited"):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))


def test_openrouter_empty_reply_is_a_failure(monkeypatch):
    settings = make_settings()
    settings.provider.fallback_to_local = False
    client = LlamaClient(settings)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": None}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(RuntimeError, match="empty message"):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))


def test_free_model_ids_filters_paid_models():
    payload = {
        "data": [
            {
                "id": "google/gemma-4-31b-it:free",
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {
                "id": "openrouter/free",
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {
                "id": "openai/gpt-6",
                "pricing": {"prompt": "0.000002", "completion": "0.00001"},
            },
            {
                "id": "google/lyria-3-pro-preview",
                "architecture": {"output_modalities": ["text", "audio"]},
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {"id": "broken"},
        ]
    }

    assert _free_model_ids(payload) == [
        "google/gemma-4-31b-it:free",
        "openrouter/free",
    ]


def test_free_model_ids_handles_junk():
    assert _free_model_ids({}) == []
    assert _free_model_ids({"data": "nope"}) == []
    assert _free_model_ids([]) == []
