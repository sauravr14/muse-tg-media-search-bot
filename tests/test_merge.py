"""Tests for title merging: files must keep their episode associations."""
import asyncio
import os
import tempfile
import unittest

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")

from app.classify import Classifier
from app.db import DB
from app.indexer import Indexer
from app.tmdb import TMDB


def file_meta(chat, msg, name="f.mkv"):
    return {"file_name": name, "size": 1, "duration": 1, "mime": "video/x-matroska",
            "tg_chat_id": chat, "tg_msg_id": msg, "caption": "", "msg_date": 1}


class FakeTMDB:
    async def match(self, p):
        return None


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.clf = Classifier(self.db)
        self.idx = Indexer(None, self.db, FakeTMDB())

    def _mk(self, title, norm, season_no=1, ep_no=1):
        t = self.db.query_one("SELECT id FROM titles WHERE norm_title=?", (norm,))
        if not t:
            cur = self.db.execute(
                "INSERT INTO titles(kind,title,norm_title,year,added_at,updated_at)"
                " VALUES('series',?,?,2026,1,1)", (title, norm))
            tid = cur.lastrowid
        else:
            tid = t["id"]
        se = self.clf.get_or_create_season(tid, season_no)
        ep = self.clf.get_or_create_episode(tid, se["id"], ep_no)
        return tid, se["id"], ep["id"]

    def test_merge_keeps_episode_links(self):
        # old junk title with S1E5 file
        t1, se1, ep1 = self._mk("Lanterns 720P", "lanterns 720p")
        f1 = self.db.execute(
            "INSERT INTO files(title_id, episode_id, season_id, file_name, tg_chat_id,"
            " tg_msg_id, indexed_at) VALUES(?,?,?,?,?,?,1)",
            (t1, ep1, se1, "a.mkv", -100, 1)).lastrowid
        # surviving canonical title with S1E1
        t2, se2, ep2 = self._mk("Lanterns", "lanterns")
        f2 = self.db.execute(
            "INSERT INTO files(title_id, episode_id, season_id, file_name, tg_chat_id,"
            " tg_msg_id, indexed_at) VALUES(?,?,?,?,?,?,1)",
            (t2, ep2, se2, "b.mkv", -100, 2)).lastrowid

        asyncio.run(self.idx._merge_titles(t1, t2))

        # old title gone
        self.assertIsNone(self.db.query_one("SELECT id FROM titles WHERE id=?", (t1,)))
        # both files live under the surviving title, episodes intact
        r1 = self.db.query_one("SELECT title_id, episode_id FROM files WHERE id=?", (f1,))
        self.assertEqual(r1["title_id"], t2)
        self.assertIsNotNone(r1["episode_id"])
        ep = self.db.query_one("SELECT ep_no, season_id FROM episodes WHERE id=?",
                               (r1["episode_id"],))
        self.assertEqual(ep["ep_no"], 5 - 4)  # ep 1
        se = self.db.query_one("SELECT season_no, title_id FROM seasons WHERE id=?",
                               (ep["season_id"],))
        self.assertEqual((se["season_no"], se["title_id"]), (1, t2))
        # no orphaned episode links anywhere
        orphans = self.db.query(
            "SELECT id FROM files WHERE episode_id IS NOT NULL AND episode_id NOT IN"
            " (SELECT id FROM episodes)")
        self.assertEqual(orphans, [])

    def test_merge_same_episode_folds_files(self):
        t1, _, ep1 = self._mk("Lanterns X", "lanterns x", ep_no=1)
        f1 = self.db.execute(
            "INSERT INTO files(title_id, episode_id, file_name, tg_chat_id, tg_msg_id,"
            " indexed_at) VALUES(?,?,?,?,?,1)", (t1, ep1, "a.mkv", -100, 1)).lastrowid
        t2, _, ep2 = self._mk("Lanterns", "lanterns", ep_no=1)
        asyncio.run(self.idx._merge_titles(t1, t2))
        r1 = self.db.query_one("SELECT episode_id FROM files WHERE id=?", (f1,))
        # folded onto the surviving episode
        self.assertEqual(r1["episode_id"], ep2)


if __name__ == "__main__":
    unittest.main()

    def test_pack_ingest(self):
        from app.parser import parse_filename
        p = parse_filename("Lanterns 2026 S01 COMBINED [E01-04] 1080p 10bit AMZN.mkv")
        res = self.clf.ingest(p, file_meta(-100, 9, "pack.mkv"))
        self.assertFalse(res.get("duplicate"))
        f = self.db.query_one("SELECT episode_id, season_id, pack_label FROM files WHERE id=?",
                              (res["file_id"],))
        self.assertIsNone(f["episode_id"])
        self.assertIsNotNone(f["season_id"])
        self.assertEqual(f["pack_label"], "E01–E04")
        se = self.db.query_one("SELECT season_no FROM seasons WHERE id=?", (f["season_id"],))
        self.assertEqual(se["season_no"], 1)
