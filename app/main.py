"""Entrypoint: runs the Telegram bot (PyroFork) and the FastAPI server together.

Single process, single container — fits a low-memory VPS and also works on
Koyeb/Render as a web service (they provide $PORT; the bot uses MTProto,
no webhook needed).
"""
from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

try:  # faster event loop (Linux/macOS); harmless if missing
    import uvloop
    uvloop.install()
except Exception:  # pragma: no cover
    pass

from .api import create_app
from .bot import MediaBot
from .config import cfg
from .db import DB
from .indexer import Indexer
from .llm import LLTitle
from .tmdb import TMDB

logging.basicConfig(
    level=getattr(logging, cfg.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("mediabot.main")


async def check_clock() -> None:
    """Warn loudly if the server clock is off.

    Pyrogram's endless "msg_id is lower than all the stored values" ->
    reconnect loop is the classic symptom of a skewed VPS clock. Compares
    local time with the HTTP Date header of Telegram's API host.
    """
    try:
        import aiohttp
        from email.utils import parsedate_to_datetime
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.head("https://api.telegram.org") as r:
                remote = parsedate_to_datetime(r.headers["Date"]).timestamp()
        skew = abs(remote - __import__("time").time())
        if skew > 3:
            log.warning("SYSTEM CLOCK IS OFF BY %.1fs - Telegram will reject messages "
                        "(msg_id errors, reconnect loops). Fix: sudo timedatectl "
                        "set-ntp true && sudo systemctl restart systemd-timesyncd", skew)
        else:
            log.info("clock OK (skew %.2fs)", skew)
    except Exception as e:
        log.info("clock check skipped: %r", e)


async def amain() -> None:
    if not cfg.bot_token:
        log.error("BOT_TOKEN is not set. See .env.example.")
        sys.exit(1)
    if not cfg.api_id or not cfg.api_hash:
        log.error("API_ID / API_HASH are required (https://my.telegram.org -> API "
                  "development tools). See .env.example.")
        sys.exit(1)
    await check_clock()
    db = DB(cfg.database_path)
    tmdb = TMDB(db, cfg.tmdb_api_key)
    bot = MediaBot(cfg, db, indexer=None)  # type: ignore[arg-type]
    llm = LLTitle(cfg.openrouter_key, cfg.llm_model) if cfg.has_llm else None
    if llm:
        log.info("LLM title fallback enabled (%s)", cfg.llm_model)
    indexer = Indexer(bot.client, db, tmdb, llm=llm)
    bot.indexer = indexer
    from .panel import IndexPanel
    bot.panel = IndexPanel(bot.client, db, indexer)

    await bot.start()
    app = create_app(cfg, db, bot_username=bot.username, bot=bot)

    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=cfg.port,
                                           log_level="warning"))
    enrich_task = asyncio.create_task(indexer.enricher_loop(), name="enricher")
    # resume interrupted scans quietly in the background
    scan_task = asyncio.create_task(indexer.scan_all(), name="resume-scan")
    # keep retrying scans that die or stall (flaky network), forever
    supervisor_task = asyncio.create_task(indexer.scan_supervisor(),
                                          name="scan-supervisor")
    try:
        await server.serve()
    finally:
        indexer.stop()
        enrich_task.cancel()
        scan_task.cancel()
        supervisor_task.cancel()
        await bot.stop()
        await tmdb.close()
        if llm:
            await llm.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
