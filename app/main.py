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
from .indexer import Indexer
from pyrogram import Client as PyroClient
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
        log.error("API_ID / API_HASH are required for the MTProto client (get them at "
                  "https://my.telegram.org -> API development tools). See .env.example.")
        sys.exit(1)
    db = DB(cfg.database_path)
    tmdb = TMDB(db, cfg.tmdb_api_key)
    bot = MediaBot(cfg, db, indexer=None)  # type: ignore[arg-type]
    history_client = None
    if cfg.has_session:
        history_client = PyroClient("history", api_id=cfg.api_id, api_hash=cfg.api_hash,
                                    session_string=cfg.session_string, in_memory=True)
        await history_client.start()
        log.info("history user-client started")
    else:
        log.warning("SESSION_STRING not set — history scans disabled; "
                    "new channel posts are still indexed live")
    indexer = Indexer(bot.client, db, tmdb, history_client=history_client)
    bot.indexer = indexer

    await bot.start()
    app = create_app(cfg, db, bot_username=bot.username)

    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=cfg.port,
                                           log_level="warning"))
    enrich_task = asyncio.create_task(indexer.enricher_loop(), name="enricher")
    # resume interrupted scans quietly in the background
    scan_task = asyncio.create_task(indexer.scan_all(), name="resume-scan")
    try:
        await server.serve()
    finally:
        indexer.stop()
        enrich_task.cancel()
        scan_task.cancel()
        await bot.stop()
        if history_client:
            await history_client.stop()
        await tmdb.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
