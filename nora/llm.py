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


def strip_thinking(text: str) -> str:
    """Remove reasoning channels and stray chat-template tokens."""
    for pattern in _THINK_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.S | re.I)
    text = _SPECIAL_TOKEN_RE.sub("", text)
    text = _NAME_PREFIX_RE.sub("", text)
    return text.strip()


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
    """Chat client for llama-server's `/v1/chat/completions` endpoint."""

    def __init__(self, settings: Settings, server: LlamaServer | None = None):
        self.settings = settings
        self.server = server or LlamaServer(settings)

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

    async def stream(
        self, messages: list[dict[str, Any]], **overrides: Any
    ) -> AsyncIterator[str]:
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
