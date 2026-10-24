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

from .api import create_app
from .bot import MediaBot
from .config import cfg
from .db import DB
from .dbthread import LoopGuard
from .indexer import Indexer
from .llm import LLTitle
from .mnimdb import MNImdb
from .tmdb import TMDB

logging.basicConfig(
    level=getattr(logging, cfg.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("mediabot.main")


async def amain() -> None:
    if not cfg.bot_token:
        log.error("BOT_TOKEN is not set. See .env.example.")
        sys.exit(1)
    if not cfg.api_id or not cfg.api_hash:
        log.error("API_ID / API_HASH are required (https://my.telegram.org -> API "
                  "development tools). See .env.example.")
        sys.exit(1)
    db = DB(cfg.database_path)
    tmdb = TMDB(db, cfg.tmdb_api_key)
    bot = MediaBot(cfg, db, indexer=None)  # type: ignore[arg-type]
    llm = LLTitle(cfg.openrouter_key, cfg.llm_model) if cfg.has_llm else None
    if llm:
        log.info("LLM title fallback enabled (%s)", cfg.llm_model)
    mnimdb = MNImdb(cfg.mn_imdb_base_url) if cfg.has_mnimdb else None
    if mnimdb:
        log.info("MN IMDb poster fallback enabled (%s)", cfg.mn_imdb_base_url)
    indexer = Indexer(bot.client, db, tmdb, llm=llm, mnimdb=mnimdb,
                      guard=LoopGuard())
    bot.indexer = indexer
    from .panel import IndexPanel
    bot.panel = IndexPanel(bot.client, db, indexer)

    await bot.start()
    app = create_app(cfg, db, bot_username=bot.username, bot=bot)

    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=cfg.port,
                                           log_level="warning"))
    await indexer.guard.start()
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
        indexer.guard.stop()
        enrich_task.cancel()
        scan_task.cancel()
        supervisor_task.cancel()
        await bot.stop()
        await tmdb.close()
        if llm:
            await llm.close()
        if mnimdb:
            await mnimdb.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
