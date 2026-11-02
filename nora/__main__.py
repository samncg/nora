"""Entry point: python -m nora [bot|chat|serve]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import config, logs
from .brain import Nora
from .keepawake import KeepAwake

log = logging.getLogger("nora")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nora",
        description="Local girlfriend with long-term memory.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="bot",
        choices=["bot", "chat", "serve", "supervisor"],
        help="bot = discord, chat = terminal, serve = model server only, "
        "supervisor = detached keepalive wrapper used by the scripts",
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument(
        "--fast", action="store_true", help="terminal chat without typing delays"
    )
    args = parser.parse_args(argv)

    settings = config.load(args.config)

    if args.command == "supervisor":
        # Runs under pythonw with no console: keep logging out of this path.
        from .supervisor import run_supervisor

        return run_supervisor()

    logs.setup(settings)
    log.info("nora starting in %s mode", args.command)

    with KeepAwake():
        if args.command == "bot":
            from .discord_bot import run_bot

            nora = Nora(settings)
            try:
                run_bot(settings, nora)
            except KeyboardInterrupt:
                pass
            finally:
                try:
                    asyncio.run(nora.close())
                except Exception:
                    pass
            return 0

        if args.command == "chat":
            from .cli import run

            nora = Nora(settings)
            try:
                asyncio.run(run(settings, nora, fast=args.fast))
            except KeyboardInterrupt:
                pass
            return 0

        return asyncio.run(_serve(settings))


async def _serve(settings) -> int:
    from .llm import LlamaServer

    server = LlamaServer(settings)
    try:
        await server.ensure_running()
        print(f"llama-server up at {server.base_url}")
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        await server.stop()


if __name__ == "__main__":
    sys.exit(main())
