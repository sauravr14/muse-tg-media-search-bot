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

    def test_poisoned_singles_hang_too_scan_still_crosses(self):
        """Real Cinemania behaviour: single-ID fetches inside the dead
        zone hang as well, so probing ids[0] of the hanging range can
        never work. The scan must probe a known-good ID outside the
        range, bisect past the zone, and still advance the checkpoint."""
        self.client.fail_on(
            lambda ids: any(120 <= i <= 140 for i in ids), "hang")
        with patch.object(indexer_mod, "GET_TIMEOUT", 0.05), \
             patch.object(indexer_mod, "POISON_TIMEOUT", 0.05):
            res = asyncio.run(self.idx.scan_source(self.chat_id, top_id=250))
        self.assertNotIn("error", res)
        self.assertGreater(res["poison_skipped"], 0)
        # only the leaves overlapping the zone are lost
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

    def test_stopindex_halts_scan_keeps_checkpoint(self):
        """/stopindex stops the scan after its current batch: status stays
        idle (supervisor won't restart it) and the checkpoint is kept."""
        async def run():
            task = asyncio.create_task(
                self.idx.scan_source(self.chat_id, top_id=250))
            # let the first batch finish, then ask for a stop
            while not self.db.query_one(
                    "SELECT last_msg_id FROM sources WHERE chat_id=?",
                    (self.chat_id,))["last_msg_id"]:
                await asyncio.sleep(0.01)
            self.idx.request_stop(self.chat_id)
            return await task
        # slow the loop down a little so the stop lands mid-scan
        with patch.object(indexer_mod, "BATCH", 50):
            res = asyncio.run(run())
        self.assertEqual(res.get("note"), "stopped by admin")
        self.assertNotIn("error", res)
        row = self.db.query_one(
            "SELECT status, last_msg_id FROM sources WHERE chat_id=?",
            (self.chat_id,))
        self.assertEqual(row["status"], "idle")
        # checkpoint kept at a completed batch, scan did not run to the end
        self.assertGreater(row["last_msg_id"], 0)
        self.assertLess(row["last_msg_id"], 250)
        self.assertFalse(self.idx._scan_lock.locked())


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


class CleanupCatalogTest(unittest.TestCase):
    """cleanup_catalog moves only files whose parse changed, preserves
    good titles (posters/TMDB), and removes orphaned junk titles."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.chat_id = -1002001978984
        self.db.upsert_source(self.chat_id, "JEROM DB", "")
        self.idx = Indexer(FakeClient({}), self.db, TMDB(self.db, ""))
        now = time.time()

        def add_title(title, norm, year, poster=""):
            cur = self.db.execute(
                "INSERT INTO titles(kind, title, norm_title, year, poster, "
                "added_at, updated_at) VALUES('movie', ?, ?, ?, ?, ?, ?)",
                (title, norm, year, poster, now, now))
            return cur.lastrowid

        def add_file(title_id, msg_id, file_name, status="ok"):
            self.db.execute(
                "INSERT INTO files(title_id, file_name, tg_chat_id, tg_msg_id, "
                "indexed_at, status) VALUES(?, ?, ?, ?, ?, ?)",
                (title_id, file_name, self.chat_id, msg_id, now, status))

        # junk title from the old parser, holding a Vivah file
        self.junk_id = add_title("Vivah 3 2Gb", "vivah 3 2gb", 2006)
        add_file(self.junk_id, 1,
                 "Vivah (2006) Hindi 1080p BluRay 3.2GB ESub [BollYFlix].mkv")
        # the correct title already exists, with a TMDB match + poster
        self.good_id = add_title("Vivah", "vivah", 2006, poster="http://p/x.jpg")
        self.db.execute("UPDATE titles SET tmdb_id=999 WHERE id=?",
                        (self.good_id,))
        add_file(self.good_id, 2, "Vivah.2006.720p.BluRay.mkv")
        # an unrelated correct title that must be left alone
        self.other_id = add_title("Inception", "inception", 2010,
                                  poster="http://p/i.jpg")
        add_file(self.other_id, 3, "Inception.2010.1080p.BluRay.mkv")

    def test_cleanup_moves_only_changed_files(self):
        res = asyncio.run(self.idx.cleanup_catalog())
        self.assertEqual(res["moved"], 1)
        self.assertEqual(res["kept"], 2)
        self.assertEqual(res["orphans_removed"], 1)
        # junk title gone
        self.assertIsNone(self.db.query_one(
            "SELECT id FROM titles WHERE id=?", (self.junk_id,)))
        # both Vivah files now under the good title, poster preserved
        files = self.db.query(
            "SELECT tg_msg_id FROM files WHERE title_id=?", (self.good_id,))
        self.assertEqual({f["tg_msg_id"] for f in files}, {1, 2})
        t = self.db.query_one("SELECT poster, tmdb_id FROM titles WHERE id=?",
                              (self.good_id,))
        self.assertEqual(t["poster"], "http://p/x.jpg")
        self.assertEqual(t["tmdb_id"], 999)
        # unrelated title untouched
        self.assertEqual(self.db.query_one(
            "SELECT COUNT(*) c FROM files WHERE title_id=?",
            (self.other_id,))["c"], 1)

    def test_cleanup_reaches_files_beyond_first_page(self):
        """Cleanup pages through the whole catalog by id: a junk-grouped
        file sitting past the first 500-file page is still moved, and its
        emptied junk title is removed."""
        now = time.time()
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO titles(kind, title, norm_title, year, "
                "added_at, updated_at) VALUES('movie', 'Bulk Movie', "
                "'bulk movie', 2020, ?, ?)", (now, now))
            bulk_id = cur.lastrowid
            cur = self.db.execute(
                "INSERT INTO titles(kind, title, norm_title, year, "
                "added_at, updated_at) VALUES('movie', 'Bulk Movie 700Mb', "
                "'bulk movie 700mb', 2020, ?, ?)", (now, now))
            junk2_id = cur.lastrowid
            for i in range(599):
                self.db.execute(
                    "INSERT INTO files(title_id, file_name, tg_chat_id, "
                    "tg_msg_id, indexed_at, status) VALUES(?, "
                    "'Bulk.Movie.2020.1080p.mkv', ?, ?, ?, 'ok')",
                    (bulk_id, self.chat_id, 1000 + i, now))
            # the junk-grouped file lands beyond the first page of 500
            self.db.execute(
                "INSERT INTO files(title_id, file_name, tg_chat_id, "
                "tg_msg_id, indexed_at, status) VALUES(?, "
                "'Bulk.Movie.2020.720p.mkv', ?, ?, ?, 'ok')",
                (junk2_id, self.chat_id, 9999, now))
        res = asyncio.run(self.idx.cleanup_catalog())
        self.assertGreaterEqual(res["moved"], 2)  # setUp junk + this one
        self.assertIsNone(self.db.query_one(
            "SELECT id FROM titles WHERE id=?", (junk2_id,)))
        f = self.db.query_one(
            "SELECT title_id FROM files WHERE tg_msg_id=9999")
        self.assertEqual(f["title_id"], bulk_id)

    def test_orphan_delete_has_cascade_indexes_and_chunks(self):
        """Orphan-title removal must not starve the event loop.

        On 2026-10-21 the single-shot orphan DELETE wedged the bot/API for
        ~1h: FK cascades into seasons/episodes had no indexes on the child
        columns, so every deleted title scanned the whole child tables.
        Guard: the three cascade indexes exist, >2000 orphans are removed
        across chunks, and seasons/episodes cascade with their title.
        """
        for name in ("idx_seasons_title", "idx_episodes_title",
                     "idx_episodes_season"):
            self.assertIsNotNone(self.db.query_one(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name=?", (name,)), f"missing index {name}")
        now = time.time()
        with self.db.transaction():
            # orphan series title with a season + episode to cascade
            cur = self.db.execute(
                "INSERT INTO titles(kind, title, norm_title, year, "
                "added_at, updated_at) VALUES('series', 'Orphan Show', "
                "'orphan show', 2021, ?, ?)", (now, now))
            series_id = cur.lastrowid
            cur = self.db.execute(
                "INSERT INTO seasons(title_id, season_no) VALUES(?, 1)",
                (series_id,))
            season_id = cur.lastrowid
            self.db.execute(
                "INSERT INTO episodes(title_id, season_id, ep_no) "
                "VALUES(?, ?, 1)", (series_id, season_id))
            # enough bare orphans to force the chunked path (>2000)
            for i in range(2100):
                self.db.execute(
                    "INSERT INTO titles(kind, title, norm_title, year, "
                    "added_at, updated_at) VALUES('movie', ?, ?, 2020, ?, ?)",
                    (f"Orphan {i}", f"orphan {i}", now, now))
        res = asyncio.run(self.idx.cleanup_catalog())
        # setUp junk (1) + series (1) + 2100 bare orphans
        self.assertEqual(res["orphans_removed"], 2102)
        self.assertIsNone(self.db.query_one(
            "SELECT id FROM titles WHERE id=?", (series_id,)))
        self.assertIsNone(self.db.query_one(
            "SELECT id FROM seasons WHERE id=?", (season_id,)))
        self.assertEqual(self.db.query_one(
            "SELECT COUNT(*) c FROM episodes WHERE title_id=?",
            (series_id,))["c"], 0)
        # good titles survive
        self.assertIsNotNone(self.db.query_one(
            "SELECT id FROM titles WHERE id=?", (self.good_id,)))
        self.assertIsNotNone(self.db.query_one(
            "SELECT id FROM titles WHERE id=?", (self.other_id,)))
