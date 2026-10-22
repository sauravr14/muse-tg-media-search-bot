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

    def test_merge_mixed_seasons_and_pack(self):
        # old title: S1 with E1 (collides) + E2 (new), S2 with E1 (no
        # target season), plus a pack file hanging on S1
        t1, se1, ep1 = self._mk("Show Junk", "show junk", season_no=1, ep_no=1)
        ep2 = self.clf.get_or_create_episode(t1, se1, 2)
        se1b = self.clf.get_or_create_season(t1, 2)
        ep_s2 = self.clf.get_or_create_episode(t1, se1b["id"], 1)
        fa = self.db.execute(
            "INSERT INTO files(title_id, episode_id, season_id, file_name,"
            " tg_chat_id, tg_msg_id, indexed_at) VALUES(?,?,?,?,?,?,1)",
            (t1, ep1, se1, "a.mkv", -100, 11)).lastrowid
        fb = self.db.execute(
            "INSERT INTO files(title_id, episode_id, season_id, file_name,"
            " tg_chat_id, tg_msg_id, indexed_at) VALUES(?,?,?,?,?,?,1)",
            (t1, ep2["id"], se1, "b.mkv", -100, 12)).lastrowid
        fc = self.db.execute(
            "INSERT INTO files(title_id, episode_id, season_id, file_name,"
            " tg_chat_id, tg_msg_id, indexed_at) VALUES(?,?,?,?,?,?,1)",
            (t1, ep_s2["id"], se1b["id"], "c.mkv", -100, 13)).lastrowid
        fp = self.db.execute(
            "INSERT INTO files(title_id, season_id, pack_label, file_name,"
            " tg_chat_id, tg_msg_id, indexed_at) VALUES(?,?,?, ?,?,?,1)",
            (t1, se1, "Complete", "pack.mkv", -100, 14)).lastrowid
        # target title: only S1E1
        t2, se2, ep_t = self._mk("Show", "show", season_no=1, ep_no=1)

        asyncio.run(self.idx._merge_titles(t1, t2))

        def link(fid):
            return self.db.query_one(
                "SELECT title_id, season_id, episode_id FROM files WHERE id=?",
                (fid,))
        # colliding episode file folded onto the target's E1
        self.assertEqual(link(fa)["episode_id"], ep_t)
        self.assertEqual(link(fa)["season_id"], se2)
        # new episode moved with its number intact
        rb = link(fb)
        epb = self.db.query_one("SELECT ep_no, season_id FROM episodes WHERE id=?",
                                (rb["episode_id"],))
        self.assertEqual((epb["ep_no"], epb["season_id"]), (2, se2))
        # non-colliding season moved wholesale
        rc = link(fc)
        sec = self.db.query_one("SELECT season_no, title_id FROM seasons WHERE id=?",
                                (rc["season_id"],))
        self.assertEqual((sec["season_no"], sec["title_id"]), (2, t2))
        # pack file followed its season, episode stays NULL
        rp = link(fp)
        self.assertEqual((rp["season_id"], rp["episode_id"]), (se2, None))
        # everything under the surviving title; no dup (season, ep) pairs
        for fid in (fa, fb, fc, fp):
            self.assertEqual(link(fid)["title_id"], t2)
        dups = self.db.query(
            "SELECT season_id, ep_no, COUNT(*) n FROM episodes"
            " WHERE title_id=? GROUP BY season_id, ep_no HAVING n > 1", (t2,))
        self.assertEqual(dups, [])
        orphans = self.db.query(
            "SELECT id FROM files WHERE episode_id IS NOT NULL AND episode_id"
            " NOT IN (SELECT id FROM episodes)")
        self.assertEqual(orphans, [])

    def test_merge_hundreds_of_episodes(self):
        # scale guard: the set-based fold must handle a long series
        t1, se1, _ = self._mk("Big Junk", "big junk", ep_no=1)
        for n in range(2, 301):
            self.clf.get_or_create_episode(t1, se1, n)
        t2, se2, _ = self._mk("Big", "big", ep_no=1)
        for n in range(2, 151):
            self.clf.get_or_create_episode(t2, se2, n)
        asyncio.run(self.idx._merge_titles(t1, t2))
        eps = self.db.query_one(
            "SELECT COUNT(*) n FROM episodes WHERE title_id=?", (t2,))
        self.assertEqual(eps["n"], 300)


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
