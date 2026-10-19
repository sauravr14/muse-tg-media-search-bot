"""Tests for scan robustness: timeouts, retries, checkpoints, heartbeats,
the auto-resume supervisor, and the robust forwarded-message filter."""
import asyncio
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")

import app.indexer as indexer_mod
from app.bot import _is_forwarded
from app.db import DB
from app.indexer import Indexer
from app.tmdb import TMDB


def make_msg(mid, chat_id, caption="Test Movie 2024 1080p"):
    media = SimpleNamespace(
        file_id=f"fid{mid}", file_unique_id=f"uniq{mid}", file_size=1000,
        width=1920, height=1080, duration=3600,
        file_name="Test.Movie.2024.1080p.mkv", mime_type="video/x-matroska",
    )
    msg = SimpleNamespace(id=mid, chat=SimpleNamespace(id=chat_id),
                          empty=False, caption=caption, date=None)
    msg.video = media
    msg.document = None
    return msg


class FakeClient:
    """Scriptable get_messages: entries can be msgs, exceptions, or 'hang'."""
    def __init__(self, msgs):
        self.msgs = msgs
        self.script = []  # list of (match_fn, action)
        self.calls = 0

    def fail_on(self, match_fn, action):
        self.script.append((match_fn, action))

    async def get_messages(self, chat_id, ids):
        self.calls += 1
        if isinstance(ids, int):
            ids = [ids]
            single = True
        else:
            single = False
        for match_fn, action in self.script:
            if match_fn(ids):
                if action == "hang":
                    await asyncio.sleep(3600)
                elif isinstance(action, Exception):
                    raise action
        out = [self.msgs.get(i, SimpleNamespace(empty=True)) for i in ids]
        return out[0] if single else out


class ScanRobustnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.chat_id = -1002001978984
        self.db.upsert_source(self.chat_id, "JEROM DB", "")
        self.msgs = {i: make_msg(i, self.chat_id) for i in range(1, 251)}
        self.client = FakeClient(self.msgs)
        self.idx = Indexer(self.client, self.db, TMDB(self.db, ""))
        self.idx._running = True  # _supervisor_tick checks this flag

    def test_hung_connection_does_not_wedge_forever(self):
        """A hung get_messages is routed around: poisoned ranges are
        bisected down to skippable leaves, the scan completes, and the
        skipped ranges are recorded instead of holding the scan lock."""
        # hang only batch fetches (multi-id); let the access probe through
        self.client.fail_on(lambda ids: len(ids) > 1, "hang")
        with patch.object(indexer_mod, "GET_TIMEOUT", 0.05), \
             patch.object(indexer_mod, "POISON_TIMEOUT", 0.05), \
             patch.object(indexer_mod, "MAX_BATCH_RETRIES", 2):
            res = asyncio.run(self.idx.scan_source(self.chat_id, top_id=250))
        self.assertNotIn("error", res)
        self.assertGreater(res["poison_skipped"], 0)
        row = self.db.query_one("SELECT status FROM sources WHERE chat_id=?",
                                (self.chat_id,))
        self.assertEqual(row["status"], "idle")
        # lock must be free for the next scan
        self.assertFalse(self.idx._scan_lock.locked())
        # poisoned ranges recorded for a later retry
        import json
        ranges = json.loads(self.db.get_kv(f"poison:{self.chat_id}"))
        self.assertTrue(ranges)

    def test_systemic_hang_still_aborts(self):
        """If even single IDs hang (systemic outage, not a poisoned range),
        _fetch_ids raises so the caller retries/aborts instead of
        silently skipping everything."""
        self.client.fail_on(lambda ids: True, "hang")
        with patch.object(indexer_mod, "GET_TIMEOUT", 0.05), \
             patch.object(indexer_mod, "POISON_TIMEOUT", 0.05):
            with self.assertRaises(asyncio.TimeoutError):
                asyncio.run(self.idx._fetch_ids(self.chat_id, [5, 6, 7], 0.05))

    def test_poisoned_subrange_skipped_rest_indexed(self):
        """Only the poisoned sub-range is skipped; everything else indexes
        normally and the checkpoint still advances past it."""
        def poisoned(ids):
            return len(ids) > 1 and any(120 <= i <= 140 for i in ids)
        self.client.fail_on(poisoned, "hang")
        # singles always work (like the real Telegram behavior)
        with patch.object(indexer_mod, "GET_TIMEOUT", 0.05), \
             patch.object(indexer_mod, "POISON_TIMEOUT", 0.05):
            res = asyncio.run(self.idx.scan_source(self.chat_id, top_id=250))
        self.assertNotIn("error", res)
        self.assertGreater(res["poison_skipped"], 0)
        # most files indexed despite the dead zone
        self.assertGreater(res["new"], 200)
        row = self.db.query_one(
            "SELECT last_msg_id, status FROM sources WHERE chat_id=?",
            (self.chat_id,))
        self.assertEqual(row["last_msg_id"], 250)
        self.assertEqual(row["status"], "idle")

    def test_flaky_batch_retried_then_succeeds(self):
        """Transient OSErrors are retried within the batch; the scan
        completes and indexes everything."""
        attempts = {"n": 0}
        orig = self.client.get_messages

        async def get_messages(chat_id, ids):
            if not isinstance(ids, int) and attempts["n"] < 2:
                attempts["n"] += 1
                raise OSError("Connection lost")
            return await orig(chat_id, ids)

        self.client.get_messages = get_messages
        res = asyncio.run(self.idx.scan_source(self.chat_id, top_id=250))
        self.assertNotIn("error", res)
        self.assertEqual(res["new"], 250)

    def test_checkpoint_is_batch_boundary_on_failure(self):
        """If batch 2 fails, the checkpoint is 100 (end of batch 1), so the
        resume re-scans from 101 instead of from 0."""
        self.client.fail_on(lambda ids: ids[0] >= 101, OSError("boom"))
        with patch.object(indexer_mod, "MAX_BATCH_RETRIES", 1):
            res = asyncio.run(self.idx.scan_source(self.chat_id, top_id=250))
        self.assertIn("error", res)
        row = self.db.query_one(
            "SELECT last_msg_id, status FROM sources WHERE chat_id=?",
            (self.chat_id,))
        self.assertEqual(row["last_msg_id"], 100)
        self.assertEqual(row["status"], "error")
        # resume picks up at 101
        self.client.script.clear()
        res2 = asyncio.run(self.idx.scan_source(self.chat_id, top_id=250))
        self.assertNotIn("error", res2)
        self.assertEqual(res2["scanned"], 150)

    def test_heartbeat_updated_during_scan(self):
        asyncio.run(self.idx.scan_source(self.chat_id, top_id=50))
        hb = self.db.get_kv(f"scan_hb:{self.chat_id}")
        self.assertTrue(hb)
        self.assertLess(time.time() - float(hb), 60)

    def test_nothing_new_clears_stale_error(self):
        self.db.set_source(self.chat_id, status="error", error="old boom",
                           last_msg_id=250)
        self.idx.note_seen(self.chat_id, 250)
        res = asyncio.run(self.idx.scan_source(self.chat_id))
        self.assertEqual(res["note"], "nothing new to scan")
        row = self.db.query_one(
            "SELECT status, error FROM sources WHERE chat_id=?", (self.chat_id,))
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["error"], "")


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.chat_id = -1002001978984
        self.db.upsert_source(self.chat_id, "JEROM DB", "")
        self.msgs = {i: make_msg(i, self.chat_id) for i in range(1, 151)}
        self.client = FakeClient(self.msgs)
        self.idx = Indexer(self.client, self.db, TMDB(self.db, ""))
        self.idx._running = True  # _supervisor_tick checks this flag

    def test_supervisor_resumes_errored_scan(self):
        self.db.set_source(self.chat_id, status="error",
                           error="OSError: Connection lost", last_msg_id=100)
        self.idx.note_seen(self.chat_id, 150)
        asyncio.run(self.idx._supervisor_tick())
        row = self.db.query_one(
            "SELECT status, last_msg_id FROM sources WHERE chat_id=?",
            (self.chat_id,))
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["last_msg_id"], 150)
        n = self.db.query_one("SELECT COUNT(*) c FROM files")["c"]
        self.assertEqual(n, 50)  # only the resumed range 101..150

    def test_supervisor_ignores_healthy_scanning(self):
        self.db.set_source(self.chat_id, status="scanning", last_msg_id=100)
        self.db.set_kv(f"scan_hb:{self.chat_id}", str(time.time()))
        with patch.object(indexer_mod, "STALL_AFTER", 1200):
            asyncio.run(self.idx._supervisor_tick())
        # untouched: no rescan happened
        self.assertEqual(self.client.calls, 0)
        row = self.db.query_one("SELECT status FROM sources WHERE chat_id=?",
                                (self.chat_id,))
        self.assertEqual(row["status"], "scanning")

    def test_supervisor_reboots_stalled_scan(self):
        self.db.set_source(self.chat_id, status="scanning", last_msg_id=100)
        self.db.set_kv(f"scan_hb:{self.chat_id}", str(time.time() - 9999))
        self.idx.note_seen(self.chat_id, 150)
        with patch.object(indexer_mod, "STALL_AFTER", 1200):
            asyncio.run(self.idx._supervisor_tick())
        row = self.db.query_one(
            "SELECT status, last_msg_id FROM sources WHERE chat_id=?",
            (self.chat_id,))
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["last_msg_id"], 150)


class ProbeTopIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.chat_id = -1002001978984
        self.db.upsert_source(self.chat_id, "JEROM DB", "")
        # channel with 12,345 messages and a few deleted near the top
        self.msgs = {i: make_msg(i, self.chat_id) for i in range(1, 12346)}
        for d in (12344, 12340, 7000):
            self.msgs[d] = SimpleNamespace(empty=True, id=d)
        self.client = FakeClient(self.msgs)
        self.idx = Indexer(self.client, self.db, TMDB(self.db, ""))

    def test_probe_finds_top(self):
        top = asyncio.run(self.idx.probe_top_id(self.chat_id))
        self.assertEqual(top, 12345)

    def test_fresh_source_scans_without_forward(self):
        """A source added via /addchannel (no top_id, no max_seen) probes
        its own top and scans the full history."""
        res = asyncio.run(self.idx.scan_source(self.chat_id))
        self.assertNotIn("error", res)
        self.assertEqual(res["scanned"], 12345)
        self.assertEqual(res["new"], 12342)  # 3 deleted
        row = self.db.query_one(
            "SELECT last_msg_id, status FROM sources WHERE chat_id=?",
            (self.chat_id,))
        self.assertEqual(row["last_msg_id"], 12345)
        self.assertEqual(row["status"], "idle")


class ForwardedFilterTest(unittest.TestCase):
    def test_forward_date(self):
        m = SimpleNamespace(forward_date=123, forward_from_chat=None,
                            forward_from=None, forward_sender_name=None)
        self.assertTrue(asyncio.run(_is_forwarded(None, None, m)))

    def test_forward_from_chat_only(self):
        # forward_date None/0 but origin present (the Pyrogram quirk)
        m = SimpleNamespace(forward_date=None,
                            forward_from_chat=SimpleNamespace(id=-1001),
                            forward_from=None, forward_sender_name=None)
        self.assertTrue(asyncio.run(_is_forwarded(None, None, m)))

    def test_forward_sender_name_only(self):
        m = SimpleNamespace(forward_date=0, forward_from_chat=None,
                            forward_from=None, forward_sender_name="anon")
        self.assertTrue(asyncio.run(_is_forwarded(None, None, m)))

    def test_not_forwarded(self):
        m = SimpleNamespace(forward_date=None, forward_from_chat=None,
                            forward_from=None, forward_sender_name=None)
        self.assertFalse(asyncio.run(_is_forwarded(None, None, m)))


if __name__ == "__main__":
    unittest.main()
