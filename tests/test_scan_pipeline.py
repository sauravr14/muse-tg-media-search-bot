"""Tests for the pipelined scan loop (fetch N+1 while ingesting N) and the
in-memory title cache in Classifier.find_title."""
import asyncio
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")

import app.indexer as indexer_mod
from app.classify import Classifier
from app.db import DB
from app.indexer import Indexer
from app.parser import parse_filename
from app.tmdb import TMDB


def make_msg(mid, chat_id, caption="Test Movie 2024 1080p"):
    media = SimpleNamespace(
        file_id=f"fid{mid}", file_unique_id=f"uniq{mid}", file_size=1000,
        width=1920, height=1080, duration=3600,
        file_name=f"Test.Movie.2024.1080p.mkv", mime_type="video/x-matroska",
    )
    msg = SimpleNamespace(id=mid, chat=SimpleNamespace(id=chat_id),
                          empty=False, caption=caption)
    msg.video = media
    msg.document = None
    msg.audio = None
    return msg


class FakeClient:
    def __init__(self, msgs, fetch_delay=0.0):
        self.msgs = msgs
        self.calls = []
        self.fetch_delay = fetch_delay

    async def get_messages(self, chat_id, ids):
        self.calls.append(ids)
        if self.fetch_delay:
            await asyncio.sleep(self.fetch_delay)
        if isinstance(ids, int):
            return self.msgs.get(ids, SimpleNamespace(empty=True))
        return [self.msgs.get(i, SimpleNamespace(empty=True)) for i in ids]


class TestPipelinedScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.chat_id = -1003812318272
        self.db.upsert_source(self.chat_id, "pipe test", "")
        self._old_batch = indexer_mod.BATCH

    def tearDown(self):
        indexer_mod.BATCH = self._old_batch

    def _scan(self, n, **kw):
        msgs = {i: make_msg(i, self.chat_id) for i in range(1, n + 1)}
        client = FakeClient(msgs, **kw)
        idx = Indexer(client, self.db, TMDB(self.db, ""))
        res = asyncio.run(idx.scan_source(self.chat_id, top_id=n))
        return res, client, idx

    def test_batches_in_order_and_checkpoint(self):
        # 250 msgs, BATCH=100 -> ranges [1,100],[101,200],[201,250];
        # 1 access probe + 3 fetches, strictly in ID order.
        res, client, _ = self._scan(250)
        self.assertNotIn("error", res)
        self.assertEqual(res["scanned"], 250)
        batch_calls = [c for c in client.calls if isinstance(c, list)]
        self.assertEqual(len(batch_calls), 3)
        self.assertEqual(batch_calls[0], list(range(1, 101)))
        self.assertEqual(batch_calls[1], list(range(101, 201)))
        self.assertEqual(batch_calls[2], list(range(201, 251)))
        row = self.db.query_one(
            "SELECT last_msg_id, status FROM sources WHERE chat_id=?",
            (self.chat_id,))
        self.assertEqual(row["last_msg_id"], 250)
        self.assertEqual(row["status"], "idle")

    def test_prefetch_overlaps_fetch_and_ingest(self):
        # fetch 0.5s + ingest 0.5s per batch, 3 batches of 25.
        # Sequential would take 3*(0.5+0.5+0.4)=4.2s (incl. 0.4s pacing
        # sleeps); pipelined must come in clearly under that.
        indexer_mod.BATCH = 25
        msgs = {i: make_msg(i, self.chat_id) for i in range(1, 76)}
        client = FakeClient(msgs, fetch_delay=0.5)
        idx = Indexer(client, self.db, TMDB(self.db, ""))
        orig_ingest = idx._ingest_batch

        def slow_ingest(m):
            time.sleep(0.5)
            return orig_ingest(m)

        idx._ingest_batch = slow_ingest
        t0 = time.monotonic()
        res = asyncio.run(idx.scan_source(self.chat_id, top_id=75))
        dt = time.monotonic() - t0
        self.assertNotIn("error", res)
        self.assertEqual(res["scanned"], 75)
        self.assertLess(dt, 3.9, f"scan took {dt:.2f}s, no overlap happening")

    def test_prefetch_failure_aborts_with_checkpoint(self):
        # Batch 2 always fails -> scan errors, but batch 1's checkpoint
        # (100) is preserved for the supervisor to resume from.
        msgs = {i: make_msg(i, self.chat_id) for i in range(1, 251)}

        class FlakyClient(FakeClient):
            async def get_messages(self, chat_id, ids):
                self.calls.append(ids)
                if isinstance(ids, list) and ids[0] >= 101:
                    raise OSError("boom")
                if isinstance(ids, int):
                    return self.msgs.get(ids, SimpleNamespace(empty=True))
                return [self.msgs.get(i, SimpleNamespace(empty=True))
                        for i in ids]

        client = FlakyClient(msgs)
        idx = Indexer(client, self.db, TMDB(self.db, ""))
        # speed up: fewer retries
        import app.indexer as im
        old_retries = im.MAX_BATCH_RETRIES
        im.MAX_BATCH_RETRIES = 2
        try:
            res = asyncio.run(idx.scan_source(self.chat_id, top_id=250))
        finally:
            im.MAX_BATCH_RETRIES = old_retries
        self.assertIn("error", res)
        row = self.db.query_one(
            "SELECT last_msg_id, status FROM sources WHERE chat_id=?",
            (self.chat_id,))
        self.assertEqual(row["last_msg_id"], 100)
        self.assertEqual(row["status"], "error")

    def test_stop_cancels_prefetch_cleanly(self):
        # Batch 2's fetch blocks; admin stops the scan while it is in
        # flight -> clean stop, checkpoint at batch 1, no dangling tasks.
        indexer_mod.BATCH = 100
        msgs = {i: make_msg(i, self.chat_id) for i in range(1, 251)}
        gate = asyncio.Event()

        class GateClient(FakeClient):
            async def get_messages(self, chat_id, ids):
                self.calls.append(ids)
                if isinstance(ids, int):
                    return self.msgs.get(ids, SimpleNamespace(empty=True))
                if ids[0] >= 101:
                    await gate.wait()
                return [self.msgs.get(i, SimpleNamespace(empty=True))
                        for i in ids]

        async def main():
            client = GateClient(msgs)
            idx = Indexer(client, self.db, TMDB(self.db, ""))
            task = asyncio.create_task(
                idx.scan_source(self.chat_id, top_id=250))
            while len([c for c in client.calls if isinstance(c, list)]) < 2:
                await asyncio.sleep(0.01)
            idx.request_stop(self.chat_id)
            gate.set()
            res = await task
            leftover = [t for t in asyncio.all_tasks()
                        if t is not asyncio.current_task()
                        and "scanfetch" in t.get_name()]
            return res, leftover

        res, leftover = asyncio.run(main())
        self.assertIn("note", res)
        self.assertEqual(res["note"], "stopped by admin")
        self.assertEqual(leftover, [])
        row = self.db.query_one(
            "SELECT last_msg_id, status FROM sources WHERE chat_id=?",
            (self.chat_id,))
        self.assertEqual(row["last_msg_id"], 100)
        self.assertEqual(row["status"], "idle")


class TestTitleCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.clf = Classifier(self.db)
        self.queries = 0
        orig_q = self.db.query
        orig_qo = self.db.query_one

        def q(*a, **k):
            self.queries += 1
            return orig_q(*a, **k)

        def qo(*a, **k):
            self.queries += 1
            return orig_qo(*a, **k)

        self.db.query = q
        self.db.query_one = qo

    def test_repeat_lookup_hits_cache(self):
        p = parse_filename("Dune.Part.Two.2024.1080p.BluRay.mkv")
        self.assertIsNone(self.clf.find_title(p))  # miss, populates negative
        n1 = self.queries
        self.assertGreater(n1, 0)
        self.clf.create_title(p, None)
        t1 = self.clf.find_title(p)
        self.assertIsNotNone(t1)
        n2 = self.queries
        t2 = self.clf.find_title(p)  # cache hit: zero DB queries
        self.assertEqual(t1["id"], t2["id"])
        self.assertEqual(self.queries, n2)

    def test_ingest_reuses_title_without_requery(self):
        meta = dict(file_name="Dune.Part.Two.2024.1080p.BluRay.mkv",
                    size=1, duration=1, mime="video/x-matroska",
                    tg_chat_id=-1, tg_msg_id=1, caption="", msg_date=0)
        p = parse_filename(meta["file_name"])
        r1 = self.clf.ingest(p, meta)
        n1 = self.queries
        meta2 = dict(meta, tg_msg_id=2,
                     file_name="Dune.Part.Two.2024.720p.BluRay.mkv")
        p2 = parse_filename(meta2["file_name"])
        r2 = self.clf.ingest(p2, meta2)
        self.assertEqual(r1["title_id"], r2["title_id"])
        # second ingest must not re-run the title lookup queries
        title_queries = self.queries - n1
        self.assertLessEqual(title_queries, 4,
                             f"title re-lookup cost {title_queries} queries")


if __name__ == "__main__":
    unittest.main()
