"""Terminal chat client - handy for testing her personality without Discord."""

from __future__ import annotations

import asyncio
import logging

from .brain import Nora
from .config import Settings
from .humanize import typing_seconds

log = logging.getLogger("nora.cli")

SCOPE = "cli:local"
BANNER = """
  nora - local girlfriend, press ctrl+c to leave.
  commands: /recall <words>   /memory   /forget <id>   /clear   /help
"""


async def run(settings: Settings, nora: Nora, *, fast: bool = False) -> None:
    print(BANNER)
    try:
        await nora.server.ensure_running()
    except Exception as exc:
        print(f"[model failed to start] {exc}")
        print("run: python scripts/fetch_model.py  and  python scripts/fetch_llama.py")
        return

    while True:
        try:
            line = await asyncio.to_thread(input, "you: ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        text = line.strip()
        if not text:
            continue
        if text.lower() in {"quit", "exit", "/quit", "/exit"}:
            break

        if text.startswith("/"):
            _handle_command(nora, text)
            continue

        chunks = await nora.reply(SCOPE, text, author_name="you")
        for chunk in chunks:
            if not fast:
                await asyncio.sleep(
                    min(1.5, typing_seconds(chunk, cps=settings.chat.typing_cps))
                )
            print(f"nora: {chunk}")

    await nora.close()


def _handle_command(nora: Nora, text: str) -> None:
    head, _, rest = text.partition(" ")
    head = head.lower()
    rest = rest.strip()
    store = nora.store_for(SCOPE)

    if head == "/recall":
        for hit in nora.recall(rest or "", limit=10, scope=SCOPE):
            stamp = str(hit.get("created_at") or "")[:16].replace("T", " ")
            print(f"  {stamp} {hit['role']:9} {hit['content'][:160]}")
    elif head == "/memory":
        for fact in store.facts(SCOPE, limit=80):
            print(f"  {fact['id']:>3}  {fact['content']}")
    elif head == "/forget":
        try:
            ok = store.forget_fact(int(rest), scope=SCOPE)
            print("  forgot." if ok else "  no such id.")
        except ValueError:
            print("  usage: /forget <id>")
    elif head == "/clear":
        n = store.forget_scope(SCOPE)
        print(f"  cleared {n} messages from this terminal thread.")
    elif head == "/help":
        print(BANNER)
    else:
        print("  unknown command")
