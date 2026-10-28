"""Keep synchronous SQLite work off the event loop.

Root cause of the 2026-10-24 all-day outages: the bot, the FastAPI server,
the enricher and the scans share ONE event loop, and every ``db.*`` call ran
synchronously on it. A slow query, a big WAL checkpoint, or a disk stall
inside any one call froze Telegram keepalives (session drops) AND the API
(healthz timeouts) at the same time, while the container itself stayed Up.

Contract (all async code must follow it):
- Any ``db.*`` call from async code goes through :func:`run_db`, which runs
  the callable on a worker thread via :func:`asyncio.to_thread`.
- A callable handed to :func:`run_db` must be SELF-CONTAINED: it may open
  its own ``db.transaction()`` and must not assume a transaction (or any
  other thread-local state) set up by another call. SQLite connections here
  are thread-local (see ``db.DB._conn``), so each worker thread safely gets
  its own connection, and writes still serialize on ``db._write_lock``.
- Never hand an open cursor across the boundary: materialize rows
  (``query``/``query_one`` already do) and return plain values
  (rowcounts, ids, dicts) — never a cursor or a lazy iterator.
- Never touch asyncio objects (queues, locks, futures) from inside a
  :func:`run_db` callable: collect results and let the loop thread act on
  them after the await returns.

:class:`LoopGuard` is the second half: a monitor that measures real event
loop lag (a blocked loop wakes its sleep late) and lets heavy jobs pause
themselves while the loop is recovering, instead of piling more work on.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import time

log = logging.getLogger("mediabot.dbthread")

# 2026-10-28 reply-latency fix: asyncio.to_thread uses the loop's default
# executor, which on this 2-vCPU VPS is only 6 threads — shared by EVERY
# run_db call in the process (bot handlers, enricher, scans, API,
# tracking). When enricher title merges held the SQLite write lock, pool
# threads piled up waiting on it and even tiny reply-path reads queued
# behind them; users saw /start and search replies arrive 3-4 minutes
# late while the container looked healthy. Two dedicated pools fix the
# sharing: a roomy general pool, and a small reply-only pool that heavy
# jobs never touch, so a reply read always has a thread immediately.
_DB_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=24, thread_name_prefix="db")
_REPLY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="db-reply")


async def run_db(func, *args, **kwargs):
    """Run a synchronous DB callable on a worker thread.

    The event loop stays free while the call runs, so a slow query or a
    disk stall delays only the caller — never Telegram keepalives, never
    /healthz, never other requests.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _DB_EXECUTOR, functools.partial(func, *args, **kwargs))


async def run_db_fast(func, *args, **kwargs):
    """Reply-path reads only (/start, search, browse, stats, Get lookups).

    Runs on the reserved reply executor, which enricher/scan/API work
    never uses — so a user command's database read cannot queue behind
    a title merge or a scan batch, no matter how busy indexing gets.
    Writes and admin/heavy reads stay on run_db; never route a write
    through here, or the reservation stops meaning anything.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _REPLY_EXECUTOR, functools.partial(func, *args, **kwargs))


class LoopGuard:
    """Measures event-loop lag and lets heavy jobs back off while lagged.

    The monitor sleeps ``interval`` seconds at a time; if the loop was
    blocked, the sleep wakes late, and the overshoot IS the lag. Heavy
    jobs call :meth:`wait_clear` before each unit of work (cleanup page,
    scan batch, enrich title) so a recovering loop is not immediately
    re-saturated. A lag above ``warn_at`` is logged — it is the earliest
    visible sign of the next stall.
    """

    def __init__(self, threshold: float = 2.0, interval: float = 1.0,
                 warn_at: float = 5.0):
        self.threshold = threshold
        self.interval = interval
        self.warn_at = warn_at
        self.lag = 0.0
        self._task: asyncio.Task | None = None
        self._warned_at = 0.0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._monitor(), name="loop-guard")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _monitor(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        try:
            while True:
                await asyncio.sleep(self.interval)
                now = loop.time()
                self.lag = max(0.0, now - last - self.interval)
                last = now
                if self.lag >= self.warn_at and now - self._warned_at > 60:
                    self._warned_at = now
                    log.warning("event loop lag %.1fs — heavy work backing off",
                                self.lag)
        except asyncio.CancelledError:
            pass

    async def wait_clear(self) -> None:
        """Sleep until the measured loop lag is back under threshold."""
        while self.lag > self.threshold:
            await asyncio.sleep(0.5)

    @staticmethod
    def now() -> float:
        return time.time()
