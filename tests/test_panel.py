"""Tests for the live /indexstatus panel and current_scan tracking."""
import asyncio
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")

from app.db import DB
from app.indexer import Indexer
from app.panel import IndexPanel, _bar, _dur
from app.tmdb import TMDB


def make_msg(mid, chat_id):
    media = SimpleNamespace(
        file_id=f"fid{mid}", file_unique_id=f"uniq{mid}", file_size=1000,
        width=1920, height=1080, duration=3600,
        file_name="Test.Movie.2024.1080p.mkv", mime_type="video/x-matroska",
    )
    msg = SimpleNamespace(id=mid, chat=SimpleNamespace(id=chat_id),
                          empty=False, caption="x", date=None)
    msg.video = media
    msg.document = None
    return msg


class FakeClient:
    def __init__(self, msgs):
        self.msgs = msgs

    async def get_messages(self, chat_id, ids):
        if isinstance(ids, int):
            return self.msgs.get(ids, SimpleNamespace(empty=True))
        return [self.msgs.get(i, SimpleNamespace(empty=True)) for i in ids]


class BarTest(unittest.TestCase):
    def test_bar(self):
        self.assertEqual(_bar(0), "░" * 12)
        self.assertEqual(_bar(100), "█" * 12)
        self.assertEqual(_bar(50), "██████" + "░" * 6)

    def test_dur(self):
        self.assertEqual(_dur(45), "45s")
        self.assertEqual(_dur(125), "2m 5s")
        self.assertEqual(_dur(3725), "1h 2m")


class CurrentScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.chat_id = -1002001978984
        self.db.upsert_source(self.chat_id, "JEROM DB", "")
        self.msgs = {i: make_msg(i, self.chat_id) for i in range(1, 251)}
        self.idx = Indexer(FakeClient(self.msgs), self.db, TMDB(self.db, ""))

    def test_current_scan_set_and_cleared(self):
        seen = {}

        def cb(done, total):
            # capture the live state mid-scan
            if self.idx.current_scan:
                seen.update(self.idx.current_scan)

        asyncio.run(self.idx.scan_source(self.chat_id, top_id=250,
                                         progress_cb=cb))
        self.assertEqual(seen["chat_id"], self.chat_id)
        self.assertEqual(seen["title"], "JEROM DB")
        self.assertEqual(seen["total"], 250)
        self.assertGreater(seen["done"], 0)
        # cleared after the scan finishes
        self.assertIsNone(self.idx.current_scan)

    def test_current_scan_cleared_on_error(self):
        class Boom(FakeClient):
            async def get_messages(self, chat_id, ids):
                raise OSError("down")

        idx = Indexer(Boom({}), self.db, TMDB(self.db, ""))
        # access_test also fails -> error path before scan starts
        res = asyncio.run(idx.scan_source(self.chat_id, top_id=10))
        self.assertIn("error", res)
        self.assertIsNone(idx.current_scan)


class PanelTextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.db.upsert_source(-1002001978984, "JEROM DB", "")
        self.db.upsert_source(-1002109202240, "Zoned Database 1", "")
        self.db.set_source(-1002109202240, status="scanning", last_msg_id=353000)
        self.idx = Indexer(None, self.db, TMDB(self.db, ""))
        self.panel = IndexPanel(client=None, db=self.db, indexer=self.idx)

    def test_idle_text(self):
        t = self.panel._text()
        self.assertIn("idle", t)
        self.assertIn("JEROM DB", t)
        self.assertIn("Zoned Database 1", t)
        self.assertIn("353,000", t)

    def test_scanning_text(self):
        self.idx.current_scan = {
            "chat_id": -1002001978984, "title": "JEROM DB",
            "done": 40000, "total": 79866, "new": 35000,
            "started_at": time.time() - 3600,
        }
        t = self.panel._text()
        self.assertIn("scanning", t)
        self.assertIn("50.1%", t)
        self.assertIn("40,000", t)
        self.assertIn("79,866", t)
        self.assertIn("39,866", t)  # remaining
        self.assertIn("35,000", t)  # new files
        self.assertIn("█", t)  # progress bar


if __name__ == "__main__":
    unittest.main()
