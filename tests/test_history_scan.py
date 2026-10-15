"""Tests for the Levii-style ID-range history scan and t.me link parsing."""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")

from app.bot import _parse_post_link
from app.db import DB
from app.indexer import Indexer
from app.tmdb import TMDB


def make_msg(mid, chat_id, kind="video", caption="Test Movie 2024 1080p"):
    media = SimpleNamespace(
        file_id=f"fid{mid}", file_unique_id=f"uniq{mid}", file_size=1000,
        width=1920, height=1080, duration=3600,
        file_name=f"Test.Movie.2024.1080p.mkv", mime_type="video/x-matroska",
    )
    msg = SimpleNamespace(id=mid, chat=SimpleNamespace(id=chat_id),
                          empty=False, caption=caption)
    setattr(msg, "video", media if kind == "video" else None)
    setattr(msg, "document", media if kind == "document" else None)
    setattr(msg, "audio", None)
    msg.media = SimpleNamespace(value=kind)
    return msg


class FakeClient:
    def __init__(self, msgs):
        self.msgs = msgs  # {id: msg}
        self.calls = []

    async def get_messages(self, chat_id, ids):
        self.calls.append(ids)
        if isinstance(ids, int):
            return self.msgs.get(ids, SimpleNamespace(empty=True))
        return [self.msgs.get(i, SimpleNamespace(empty=True)) for i in ids]


class TestLinkParsing(unittest.TestCase):
    def test_public_link(self):
        self.assertEqual(_parse_post_link("https://t.me/somechannel/123"),
                         ("@somechannel", 123))

    def test_private_link(self):
        self.assertEqual(_parse_post_link("https://t.me/c/3812318272/456"),
                         (-1003812318272, 456))

    def test_bare_link(self):
        self.assertEqual(_parse_post_link("t.me/mychan/7"), ("@mychan", 7))

    def test_no_match(self):
        self.assertIsNone(_parse_post_link("hello world"))
        self.assertIsNone(_parse_post_link("https://t.me/somechannel"))


class TestIdRangeScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.chat_id = -1003812318272
        self.db.upsert_source(self.chat_id, "yokohama dump", "")
        msgs = {i: make_msg(i, self.chat_id, caption=f"Film {i} 2024 720p")
                for i in range(1, 501)}
        msgs[250] = SimpleNamespace(empty=True)  # deleted message
        self.client = FakeClient(msgs)
        self.idx = Indexer(self.client, self.db, TMDB(self.db, ""))

    def test_scan_batches_and_checkpoint(self):
        res = asyncio.run(self.idx.scan_source(self.chat_id, top_id=500))
        self.assertNotIn("error", res)
        self.assertEqual(res["scanned"], 500)
        # 499 media messages (one deleted)
        self.assertEqual(res["new"], 499)
        # 1 access-test probe + 5 batches of 100
        self.assertEqual(len(self.client.calls), 6)
        row = self.db.query_one("SELECT last_msg_id, status FROM sources WHERE chat_id=?",
                                (self.chat_id,))
        self.assertEqual(row["last_msg_id"], 500)
        self.assertEqual(row["status"], "idle")

    def test_resume_from_checkpoint(self):
        asyncio.run(self.idx.scan_source(self.chat_id, top_id=200))
        self.client.calls.clear()
        res = asyncio.run(self.idx.scan_source(self.chat_id, top_id=500))
        self.assertEqual(res["scanned"], 300)
        self.assertEqual(self.db.query_one(
            "SELECT last_msg_id FROM sources WHERE chat_id=?",
            (self.chat_id,))["last_msg_id"], 500)

    def test_access_test_failure(self):
        class ChannelPrivate(Exception):
            pass

        class BadClient(FakeClient):
            async def get_messages(self, chat_id, ids):
                raise ChannelPrivate("private")
        idx = Indexer(BadClient({}), self.db, TMDB(self.db, ""))
        ok, reason = asyncio.run(idx.access_test(self.chat_id))
        self.assertFalse(ok)
        self.assertIn("admin", reason)

    def test_duplicate_ingest_idempotent(self):
        asyncio.run(self.idx.scan_source(self.chat_id, top_id=10))
        n1 = self.db.query_one("SELECT COUNT(*) c FROM files")["c"]
        asyncio.run(self.idx.scan_source(self.chat_id, top_id=10))
        n2 = self.db.query_one("SELECT COUNT(*) c FROM files")["c"]
        self.assertEqual(n1, n2)


if __name__ == "__main__":
    unittest.main()
