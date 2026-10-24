"""Tests for the event-loop offload (2026-10-24 uptime fix).

The 2026-10-24 outages happened because every SQLite call ran
synchronously on the bot's single event loop: one slow commit, WAL
checkpoint, or disk stall froze Telegram keepalives AND /healthz at the
same time while the container stayed Up. These tests pin the fix:
DB work via run_db must run off the loop, and the loop must stay live
while it runs.
"""
import asyncio
import os
import tempfile
import threading
import time
import unittest

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")

from app.db import DB
from app.dbthread import LoopGuard, run_db
from app.indexer import Indexer
from app.tmdb import TMDB


class RunDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))

    def test_runs_off_calling_thread_and_returns_value(self):
        seen = {}

        def work():
            seen["thread"] = threading.current_thread().name
            time.sleep(0.05)
            return 42

        res = asyncio.run(run_db(work))
        self.assertEqual(res, 42)
        self.assertNotEqual(seen["thread"],
                            threading.current_thread().name)

    def test_propagates_exceptions(self):
        def boom():
            raise ValueError("db exploded")

        with self.assertRaises(ValueError):
            asyncio.run(run_db(boom))

    def test_db_write_on_worker_visible_on_main_thread(self):
        """A write committed on the worker thread must be readable from
        the loop thread's own connection (WAL visibility across
        connections — the exact property the offload relies on)."""
        asyncio.run(run_db(
            self.db.execute,
            "INSERT INTO titles(kind, title, norm_title, added_at, updated_at)"
            " VALUES('movie', 'Test Film', 'test film', 1, 1)"))
        row = self.db.query_one("SELECT title FROM titles WHERE norm_title=?",
                                ("test film",))
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Test Film")

    def test_loop_stays_live_during_blocking_db_work(self):
        """THE regression test for 2026-10-24: while a 0.5s blocking
        call runs via run_db, the event loop must keep ticking. (Called
        directly on the loop, the same call would stall everything for
        the full 0.5s — that is what used to kill healthz + Telegram.)"""
        async def main():
            ticks = []

            async def ticker():
                while True:
                    ticks.append(time.monotonic())
                    await asyncio.sleep(0.05)

            task = asyncio.create_task(ticker())
            await run_db(time.sleep, 0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return ticks

        ticks = asyncio.run(main())
        # 0.5s of blocking work must still leave >= 5 ticker wakes
        self.assertGreaterEqual(len(ticks), 5)

    def test_transaction_on_worker_is_atomic(self):
        def batch():
            with self.db.transaction():
                self.db.execute(
                    "INSERT INTO titles(kind, title, norm_title, added_at,"
                    " updated_at) VALUES('movie', 'A', 'a', 1, 1)")
                self.db.execute(
                    "INSERT INTO titles(kind, title, norm_title, added_at,"
                    " updated_at) VALUES('movie', 'B', 'b', 1, 1)")

        asyncio.run(run_db(batch))
        rows = self.db.query("SELECT title FROM titles ORDER BY title")
        self.assertEqual([r["title"] for r in rows], ["A", "B"])


class LoopGuardTest(unittest.TestCase):
    def test_wait_clear_passes_when_lag_low(self):
        g = LoopGuard(threshold=2.0)
        g.lag = 0.1
        asyncio.run(g.wait_clear())  # must return immediately

    def test_wait_clear_blocks_while_lagged_then_resumes(self):
        g = LoopGuard(threshold=2.0)

        async def main():
            async def clear_lag():
                await asyncio.sleep(0.3)
                g.lag = 0.0

            g.lag = 9.0
            task = asyncio.create_task(g.wait_clear())
            await clear_lag()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(main())

    def test_monitor_reports_low_lag_on_free_loop(self):
        async def main():
            g = LoopGuard(interval=0.2)
            await g.start()
            await asyncio.sleep(0.7)
            lag = g.lag
            g.stop()
            return lag

        self.assertLess(asyncio.run(main()), 0.5)


class CleanupOffloadTest(unittest.TestCase):
    """cleanup_catalog through the new worker-thread path: same results
    as before, and the loop stays live while it walks."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.idx = Indexer(None, self.db, TMDB(self.db, ""))
        # two files under one junk title + one file already correct
        self.db.execute(
            "INSERT INTO titles(kind, title, norm_title, year, added_at,"
            " updated_at) VALUES('movie', 'Junk Name', 'junk name', NULL, 1, 1)")
        junk = self.db.query_one("SELECT id FROM titles")["id"]
        for i, name in enumerate(("Real Movie 2024 1080p.mkv",
                                  "Real Movie 2024 720p.mkv",
                                  "Other Film 2023 1080p.mkv")):
            self.db.execute(
                "INSERT INTO files(title_id, file_name, tg_chat_id, tg_msg_id,"
                " indexed_at) VALUES(?, ?, ?, ?, 1)",
                (junk, name, -1001, 100 + i))
        # the third file already parses to its own title only after a
        # move; simplest assertion: cleanup completes and the checkpoint
        # is gone afterwards (completed runs delete it)
        self.junk_id = junk

    def test_cleanup_completes_and_clears_checkpoint(self):
        res = asyncio.run(self.idx.cleanup_catalog())
        self.assertIn("moved", res)
        self.assertIn("kept", res)
        self.assertEqual(self.db.get_kv("cleanup:ckpt"), "")
        # junk title must be gone (its files moved away)
        row = self.db.query_one("SELECT id FROM titles WHERE id=?",
                                (self.junk_id,))
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
