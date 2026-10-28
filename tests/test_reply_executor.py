"""Reply-latency guard (2026-10-28): /start and search replies arrived
3-4 minutes late while the container looked healthy. One contributor was
asyncio.to_thread's default pool: only 6 threads on this 2-vCPU VPS,
shared by every run_db call (bot handlers, enricher, scans, API,
tracking). Reply reads must never queue behind heavy indexing work, so
they run on a reserved executor that heavy jobs never use."""

import asyncio
import pathlib

from app import dbthread

BOT_SRC = pathlib.Path(__file__).parent.parent.joinpath("app", "bot.py").read_text()


def test_executors_are_separate_and_sized():
    assert dbthread._DB_EXECUTOR is not dbthread._REPLY_EXECUTOR
    assert dbthread._DB_EXECUTOR._max_workers == 24
    assert dbthread._REPLY_EXECUTOR._max_workers == 8


def test_both_executors_run_work():
    assert asyncio.run(dbthread.run_db(lambda: 1 + 1)) == 2
    assert asyncio.run(dbthread.run_db_fast(lambda: 2 + 2)) == 4


def test_reply_paths_use_fast_executor():
    # _unjoined feeds every reply path; _send_start and _do_search are the
    # two commands the user saw delayed.
    for fn in ("_unjoined", "_send_start", "_do_search"):
        body = BOT_SRC.split(f"async def {fn}")[1].split("async def ")[0]
        assert "run_db_fast" in body, fn
    # Writes must NOT move to the fast pool, or the reservation is void.
    assert "run_db(self.db.upsert_user" in BOT_SRC
    assert "run_db(owner_mod.set_start_photo" in BOT_SRC
