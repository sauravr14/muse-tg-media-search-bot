"""deliver_to_user: cached file_id fast path, fallback copy, file_id backfill."""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")

from app.bot import MediaBot
from app.config import Config
from app.db import DB


class FakeClient:
    def __init__(self, cached_ok=True):
        self.calls, self.cached_ok = [], cached_ok

    async def send_cached_media(self, uid, fid, **kw):
        self.calls.append(("cached", fid))
        if not self.cached_ok:
            raise RuntimeError("FileIdInvalid")
        return SimpleNamespace(id=100)

    async def get_messages(self, chat, mid):
        self.calls.append(("get", mid))
        async def copy(uid, **kw):
            self.calls.append(("copy", uid))
            return SimpleNamespace(id=101, video=SimpleNamespace(file_id="NEWFID"))
        return SimpleNamespace(empty=False, copy=copy)

    async def send_message(self, uid, text, **kw):
        return SimpleNamespace(id=102)


class DeliveryTest(unittest.TestCase):
    def make(self, file_id, cached_ok=True):
        db = DB(os.path.join(tempfile.mkdtemp(), "d.db"))
        db.execute("INSERT INTO files(file_name,tg_chat_id,tg_msg_id,indexed_at,tg_file_id)"
                   " VALUES('a.mkv',-1,5,1,?)", (file_id,))
        bot = MediaBot.__new__(MediaBot)
        bot.cfg, bot.db, bot.client = Config(), db, FakeClient(cached_ok)
        bot._schedule_delete = lambda *a, **k: None
        return bot, db

    def run_deliver(self, bot):
        async def go():
            r = await bot.deliver_to_user(7, 1)
            await asyncio.sleep(0)
            return r
        return asyncio.run(go())

    def test_fast_path_single_call(self):
        bot, _ = self.make("CACHED")
        self.assertEqual(self.run_deliver(bot), (True, "sent"))
        self.assertEqual(bot.client.calls, [("cached", "CACHED")])  # no get_messages

    def test_fallback_copies_and_backfills_file_id(self):
        bot, db = self.make("")
        self.assertEqual(self.run_deliver(bot), (True, "sent"))
        self.assertEqual([c[0] for c in bot.client.calls], ["get", "copy"])
        self.assertEqual(db.query_one("SELECT tg_file_id f FROM files")["f"], "NEWFID")

    def test_stale_cached_id_falls_back(self):
        bot, _ = self.make("STALE", cached_ok=False)
        self.assertEqual(self.run_deliver(bot), (True, "sent"))
        self.assertEqual([c[0] for c in bot.client.calls], ["cached", "get", "copy"])


if __name__ == "__main__":
    unittest.main()
