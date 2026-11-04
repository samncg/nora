"""llama.cpp server lifecycle plus a thin OpenAI-compatible chat client."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from typing import Any, AsyncIterator

import httpx

from .config import Settings

log = logging.getLogger("nora.llm")

_THINK_PATTERNS = [
    r"<\|channel>thought.*?<channel\|>",
    r"<start_of_turn>thought.*?<end_of_turn>",
    r"<\|think\|>.*?<\|/think\|>",
    r"<\|channel\|>thought.*?<\|/channel\|>",
]
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|<>\n]{1,40}\|>")
_NAME_PREFIX_RE = re.compile(r"^\s*(?:nora|assistant)\s*:\s*", re.I)

_OPENROUTER_ERRORS = {
    401: "OpenRouter rejected the API key (401). Update it from the tray icon.",
    402: "OpenRouter says this account is out of credits (402).",
    429: "OpenRouter rate-limited the request (429). Try again in a bit.",
}
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def strip_thinking(text: str) -> str:
    """Remove reasoning channels and stray chat-template tokens."""
    for pattern in _THINK_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.S | re.I)
    text = _SPECIAL_TOKEN_RE.sub("", text)
    text = _NAME_PREFIX_RE.sub("", text)
    return text.strip()


def _openrouter_error_text(resp: Any) -> str:
    """Turn an OpenRouter error response into something worth logging."""
    base = _OPENROUTER_ERRORS.get(
        resp.status_code, f"OpenRouter returned HTTP {resp.status_code}."
    )
    try:
        error = resp.json().get("error") or {}
    except Exception:
        error = {}
    message = error.get("message") if isinstance(error, dict) else None
    metadata = error.get("metadata") if isinstance(error, dict) else None
    hint = metadata.get("raw") if isinstance(metadata, dict) else None
    parts = [base]
    if message and str(message) not in base:
        parts.append(str(message))
    if hint:
        parts.append(str(hint))
    return " ".join(part for part in parts if part).strip()[:600]


def _free_model_ids(payload: Any) -> list[str]:
    """Pull free model ids out of an OpenRouter /models response."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or model_id in ids:
            continue
        outputs = (item.get("architecture") or {}).get("output_modalities")
        if isinstance(outputs, list) and any(mode != "text" for mode in outputs):
            # Skip music/image endpoints: the picker is for chat models.
            continue
        pricing = item.get("pricing") or {}
        free_price = (
            isinstance(pricing, dict)
            and str(pricing.get("prompt")) in {"0", "0.0"}
            and str(pricing.get("completion")) in {"0", "0.0"}
        )
        if model_id.endswith(":free") or free_price:
            ids.append(model_id)
    return sorted(ids)


def openrouter_free_models(timeout: float = 10.0) -> list[str]:
    """Free model ids from OpenRouter, best effort (empty list when offline)."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(_OPENROUTER_MODELS_URL)
        resp.raise_for_status()
        return _free_model_ids(resp.json())
    except Exception:
        log.warning("could not fetch the OpenRouter model list", exc_info=True)
        return []


def probe_openrouter(settings: Settings, timeout: float = 20.0) -> tuple[bool, str]:
    """Send one tiny message so the tray can report whether the key works."""
    cfg = settings.provider.openrouter
    key = (cfg.api_key or "").strip()
    model = (cfg.model or "").strip()
    if not key:
        return False, "no API key set"
    if not model:
        return False, "no model selected"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": cfg.referer,
        "X-Title": cfg.title,
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 4,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{cfg.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
    except Exception as exc:
        return False, f"could not reach openrouter: {exc}"
    if resp.status_code == 200:
        return True, f"{model} replied"
    return False, _openrouter_error_text(resp)


class LlamaServer:
    """Owns a llama-server process so Nora stays up without manual babysitting."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._proc: asyncio.subprocess.Process | None = None
        self._log_handle: Any = None

    @property
    def base_url(self) -> str:
        host = self.settings.server.host
        return f"http://{host}:{self.settings.server.port}"

    def build_command(self) -> list[str]:
        model = self.settings.resolve_model_path()
        m, s = self.settings.model, self.settings.server
        cmd = [
            str(self.settings.path(s.binary)),
            "-m",
            str(model),
            "-c",
            str(m.context),
            "-t",
            str(m.threads),
            "-ngl",
            str(m.gpu_layers),
            "--host",
            s.host,
            "--port",
            str(s.port),
            "--jinja",
        ]
        if m.flash_attn:
            cmd.extend(["-fa", "on"])
        mmproj = self.settings.resolve_mmproj_path()
        if mmproj is not None:
            cmd.extend(["--mmproj", str(mmproj)])
        cmd.extend(m.extra_args)
        return cmd

    async def healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.base_url}/health")
            return resp.status_code == 200
        except Exception:
            return False

    async def ensure_running(self) -> None:
        if not self.settings.server.manage:
            if not await self._wait_ready(self.settings.server.startup_timeout):
                raise RuntimeError(
                    f"llama-server is not reachable at {self.base_url}. "
                    "Start it yourself or set server.manage: true in config.yaml."
                )
            return

        if await self.healthy():
            return

        if self._proc is not None and self._proc.returncode is None:
            await self._wait_ready(self.settings.server.startup_timeout)
            return

        # A previous server died: clean up before spawning another one.
        if self._proc is not None:
            self._proc = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

        binary = self.settings.path(self.settings.server.binary)
        if not binary.exists():
            raise FileNotFoundError(
                f"llama-server not found at {binary}.\n"
                "Run: python scripts/fetch_llama.py"
            )

        log_file = self.settings.path(self.settings.server.log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = open(log_file, "ab")
        cmd = self.build_command()
        log.info("starting llama-server: %s", " ".join(cmd))

        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=self._log_handle,
            stderr=self._log_handle,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        await self._wait_ready(self.settings.server.startup_timeout)

    async def _wait_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.returncode is not None:
                raise RuntimeError(
                    "llama-server exited immediately. Check "
                    f"{self.settings.server.log_file} for the reason."
                )
            if await self.healthy():
                log.info("llama-server ready at %s", self.base_url)
                return True
            await asyncio.sleep(1.0)
        log.error("llama-server did not become ready in %.0fs", timeout)
        return False

    async def stop(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            log.info("stopping llama-server")
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=15)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


class LlamaClient:
    """Chat client for llama-server or OpenRouter's chat completions API."""

    def __init__(self, settings: Settings, server: LlamaServer | None = None):
        self.settings = settings
        self.server = server or LlamaServer(settings)
        # After a 429, skip OpenRouter for a bit instead of retrying every text.
        self._openrouter_blocked_until = 0.0

    @property
    def provider(self) -> str:
        return (self.settings.provider.mode or "local").strip().lower()

    def _payload(self, messages: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
        m = self.settings.model
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": overrides.get("temperature", m.temperature),
            "top_p": overrides.get("top_p", m.top_p),
            "top_k": overrides.get("top_k", m.top_k),
            "repeat_penalty": overrides.get("repeat_penalty", m.repeat_penalty),
            "max_tokens": overrides.get("max_tokens", m.max_tokens),
            "stream": False,
            "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": m.enable_thinking},
        }
        stop = overrides.get("stop") or []
        if stop:
            payload["stop"] = stop
        return payload

    async def chat(self, messages: list[dict[str, Any]], **overrides: Any) -> str:
        if self.provider != "openrouter":
            return await self._local_chat(messages, **overrides)
        if (
            self.settings.provider.fallback_to_local
            and time.monotonic() < self._openrouter_blocked_until
        ):
            return await self._local_chat(messages, **overrides)
        try:
            return await self._openrouter_chat(messages, **overrides)
        except (RuntimeError, httpx.HTTPError) as exc:
            if not self.settings.provider.fallback_to_local:
                raise
            if "429" in str(exc):
                self._openrouter_blocked_until = time.monotonic() + 60.0
                log.warning(
                    "openrouter rate-limited; using the local model for 60s"
                )
            else:
                log.warning(
                    "openrouter failed, answering with the local model instead: %s",
                    exc,
                )
            return await self._local_chat(messages, **overrides)

    async def _local_chat(
        self, messages: list[dict[str, Any]], **overrides: Any
    ) -> str:
        await self.server.ensure_running()
        payload = self._payload(messages, **overrides)
        timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
        url = f"{self.server.base_url}/v1/chat/completions"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 400 and "chat_template_kwargs" in payload:
                # Older llama.cpp builds do not know this field.
                payload.pop("chat_template_kwargs", None)
                resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected completion payload: {data!r}") from exc
        return strip_thinking(content)

    # -- openrouter ------------------------------------------------------
    def _openrouter_headers(self) -> dict[str, str]:
        cfg = self.settings.provider.openrouter
        key = (cfg.api_key or "").strip()
        if not key:
            raise RuntimeError(
                "OpenRouter is selected but no API key is set. "
                "Set one from the tray icon or put OPENROUTER_API_KEY in .env."
            )
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": cfg.referer,
            "X-Title": cfg.title,
        }

    def openrouter_payload(
        self, messages: list[dict[str, Any]], **overrides: Any
    ) -> dict[str, Any]:
        """Only standard OpenAI fields: llama.cpp options are not portable."""
        cfg = self.settings.provider.openrouter
        model = (cfg.model or "").strip()
        if not model:
            raise RuntimeError(
                "OpenRouter is selected but no model is set. "
                "Pick one from the tray icon."
            )
        m = self.settings.model
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": overrides.get("temperature", m.temperature),
            "top_p": overrides.get("top_p", m.top_p),
            "max_tokens": overrides.get("max_tokens", m.max_tokens),
            "stream": False,
            # She is texting, not solving proofs. Reasoning models otherwise
            # spend the whole max_tokens budget thinking and return no words.
            "reasoning": {"enabled": False},
        }
        stop = overrides.get("stop") or []
        if stop:
            payload["stop"] = stop
        return payload

    async def _openrouter_chat(
        self, messages: list[dict[str, Any]], **overrides: Any
    ) -> str:
        cfg = self.settings.provider.openrouter
        url = f"{cfg.base_url.rstrip('/')}/chat/completions"
        payload = self.openrouter_payload(messages, **overrides)
        headers = self._openrouter_headers()
        timeout = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 429:
                # Free models share a busy upstream pool; one quick retry helps.
                log.info("openrouter 429, retrying once")
                await asyncio.sleep(2.0)
                resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(_openrouter_error_text(resp))
        data = resp.json()
        error = data.get("error") if isinstance(data, dict) else None
        if error and not data.get("choices"):
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeError(f"OpenRouter: {message}")
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected OpenRouter payload: {data!r}") from exc
        content = strip_thinking(content)
        if not content:
            raise RuntimeError("OpenRouter returned an empty message.")
        return content

    async def stream(
        self, messages: list[dict[str, Any]], **overrides: Any
    ) -> AsyncIterator[str]:
        if self.provider == "openrouter":
            async for piece in self._openrouter_stream(messages, **overrides):
                yield piece
            return
        await self.server.ensure_running()
        payload = self._payload(messages, **overrides)
        payload["stream"] = True
        timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{self.server.base_url}/v1/chat/completions", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    try:
                        delta = data["choices"][0]["delta"].get("content") or ""
                    except (KeyError, IndexError, TypeError):
                        continue
                    if delta:
                        yield delta

    async def _openrouter_stream(
        self, messages: list[dict[str, Any]], **overrides: Any
    ) -> AsyncIterator[str]:
        cfg = self.settings.provider.openrouter
        url = f"{cfg.base_url.rstrip('/')}/chat/completions"
        payload = self.openrouter_payload(messages, **overrides)
        payload["stream"] = True
        headers = self._openrouter_headers()
        timeout = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", url, json=payload, headers=headers
            ) as resp:
                if resp.status_code in _OPENROUTER_ERRORS:
                    raise RuntimeError(_OPENROUTER_ERRORS[resp.status_code])
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    try:
                        delta = data["choices"][0]["delta"].get("content") or ""
                    except (KeyError, IndexError, TypeError):
                        continue
                    if delta:
                        yield delta
