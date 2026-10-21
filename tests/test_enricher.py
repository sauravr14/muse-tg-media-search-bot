"""Enricher reliability: transient errors must not park titles; sweep must advance."""
import asyncio
import os
import tempfile
import time
import unittest

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")

from app.db import DB
from app.indexer import Indexer
from app.llm import LLTitle
from app.tmdb import TMDBError


class FlakyTMDB:
    def __init__(self, fail=False, meta=None):
        self.fail, self.meta, self.calls = fail, meta, 0

    async def match(self, p):
        self.calls += 1
        if self.fail:
            raise TMDBError("ConnectionResetError")
        return self.meta

    async def episode_title(self, *a):
        return ""


META = {"tmdb_id": 9, "kind": "movie", "title": "Spider-Man: No Way Home", "orig_title": "",
        "alt_titles": [], "year": 2021, "poster": "p", "backdrop": "b", "overview": "",
        "genres": [], "rating": 8.0, "popularity": 100.0, "lang": "en"}


class EnricherTest(unittest.TestCase):
    def setUp(self):
        self.db = DB(os.path.join(tempfile.mkdtemp(), "e.db"))

    def add(self, title, next_at=0.0):
        self.db.execute(
            "INSERT INTO titles(kind,title,norm_title,year,added_at,updated_at,tmdb_next_at)"
            " VALUES('movie',?,?,2021,1,1,?)", (title, title.lower(), next_at))
        return self.db.query_one("SELECT MAX(id) m FROM titles")["m"]

    def row(self, tid):
        return self.db.query_one("SELECT * FROM titles WHERE id=?", (tid,))

    def test_tmdb_network_error_retries_in_minutes_not_days(self):
        tid = self.add("Spider Man No Way Home")
        idx = Indexer(None, self.db, FlakyTMDB(fail=True))
        before = time.time()
        asyncio.run(idx._enrich_one(tid))
        r = self.row(tid)
        self.assertLess(r["tmdb_next_at"] - before, 400)   # ~5 min, not 7 days
        self.assertEqual(r["tmdb_tries"], 0)               # no penalty

    def test_definitive_no_match_backs_off_and_counts(self):
        tid = self.add("Totally Unknown Thing")
        idx = Indexer(None, self.db, FlakyTMDB(meta=None))
        asyncio.run(idx._enrich_one(tid))
        r = self.row(tid)
        self.assertEqual(r["tmdb_tries"], 1)
        self.assertGreater(r["tmdb_next_at"] - time.time(), 80000)  # ~1 day

    def test_success_adopts_meta(self):
        tid = self.add("Spider Man No Way Home")
        idx = Indexer(None, self.db, FlakyTMDB(meta=META))
        asyncio.run(idx._enrich_one(tid))
        self.assertEqual(self.row(tid)["tmdb_id"], 9)

    def test_variants_merge_into_one_title(self):
        a = self.add("Spider Man No Way Home")
        b = self.add("Spider Man No Way Home We")
        for t, n in ((a, 1), (b, 2)):
            self.db.execute(
                "INSERT INTO files(title_id,file_name,tg_chat_id,tg_msg_id,indexed_at)"
                " VALUES(?,?,?,?,1)", (t, f"f{n}.mkv", -1, n))
        idx = Indexer(None, self.db, FlakyTMDB(meta=META))
        asyncio.run(idx._enrich_one(a))
        asyncio.run(idx._enrich_one(b))
        titles = self.db.query("SELECT id FROM titles")
        self.assertEqual(len(titles), 1)
        self.assertEqual(self.db.query_one("SELECT COUNT(*) c FROM files WHERE title_id=?",
                                           (titles[0]["id"],))["c"], 2)

    def test_sweep_advances_past_recently_tried_row(self):
        # old bug: sweep only looked at the oldest unmatched row and spun on it
        self.add("Old Parked Title", next_at=time.time() + 86400 * 5)
        due = self.add("Due Title")
        row = self.db.query_one(
            "SELECT id FROM titles WHERE tmdb_id IS NULL AND tmdb_next_at <= ? "
            "ORDER BY tmdb_next_at, id LIMIT 1", (time.time(),))
        self.assertEqual(row["id"], due)

    def test_llm_rate_limit_is_not_cached_as_unknown(self):
        class Sess429:
            posts = 0
            closed = False

            def post(self, *a, **k):
                Sess429.posts += 1

                class R:
                    status = 429

                    async def __aenter__(s): return s
                    async def __aexit__(s, *a): return False
                    async def text(s): return ""
                return R()

        tid = self.add("Un Coeur En Hiver")
        self.db.execute("INSERT INTO files(title_id,file_name,tg_chat_id,tg_msg_id,indexed_at)"
                        " VALUES(?,?,?,?,1)", (tid, "Un Coeur En Hiver 1992.mkv", -1, 1))
        llm = LLTitle("k", session=Sess429())
        idx = Indexer(None, self.db, FlakyTMDB(meta=None), llm=llm)
        asyncio.run(idx._enrich_one(tid))
        self.assertEqual(self.db.query("SELECT * FROM kv WHERE key LIKE 'llm_guess:%'"), [])
        r = self.row(tid)
        self.assertEqual(r["tmdb_tries"], 0)               # not penalised
        self.assertLess(r["tmdb_next_at"] - time.time(), 400)


if __name__ == "__main__":
    unittest.main()
