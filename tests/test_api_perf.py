"""Catalog API: dedupe of unmatched variants, /api/home, index usage."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Config
from app.db import DB

POSTER = "https://image.tmdb.org/t/p/w500/x.jpg"


def add(db, title, norm=None, tmdb=None, year=2021, kind="movie", poster="", pop=None, n=[0]):
    n[0] += 1
    db.execute(
        "INSERT INTO titles(kind,title,norm_title,year,tmdb_id,poster,backdrop,overview,"
        "popularity,rating,added_at,updated_at,last_file_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, title, norm or title.lower(), year, tmdb, poster,
         "https://image.tmdb.org/t/p/w500/b.jpg" if tmdb else "", "ov" if tmdb else "",
         pop, 7.5 if tmdb else None, 1, 1, 1000 + n[0]))


class ApiPerfTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = DB(Path(tempfile.mkdtemp()) / "p.db")
        add(cls.db, "Spider-Man: No Way Home", "spider man no way home", tmdb=634649,
            poster=POSTER, pop=300)
        # the screenshot bug: unmatched junk variants of the same movie
        for junk in ("We", "BI", "X2", "X26", "Web"):
            add(cls.db, f"Spider Man No Way Home {junk}", f"spider man no way home {junk.lower()}")
        # obscure title TMDB can't match: must stay findable (once)
        add(cls.db, "Kuttyweb Rare Film", "kuttyweb rare film")
        add(cls.db, "Kuttyweb Rare Film Hdrip", "kuttyweb rare film hdrip")
        cfg = Config()
        cfg.bot_token = "t"
        cls.client = TestClient(create_app(cfg, cls.db, bot_username="b"))

    def titles(self, q, **kw):
        r = self.client.get("/api/search", params={"q": q, **kw})
        self.assertEqual(r.status_code, 200)
        return [t["title"] for t in r.json()["results"]]

    def test_no_way_home_is_one_card(self):
        self.assertEqual(self.titles("no way home"), ["Spider-Man: No Way Home"])

    def test_unmatched_obscure_title_found_once(self):
        self.assertEqual(self.titles("kuttyweb"), ["Kuttyweb Rare Film"])

    def test_punctuation_in_query_is_safe(self):
        self.assertEqual(self.titles("spider-man: no way"), ["Spider-Man: No Way Home"])
        self.assertEqual(self.titles('"; DROP TABLE titles; --'), [])

    def test_recent_and_browse_hide_unmatched(self):
        r = self.client.get("/api/recent").json()["results"]
        self.assertEqual([t["title"] for t in r], ["Spider-Man: No Way Home"])
        b = self.client.get("/api/browse", params={"kind": "movie"}).json()
        self.assertEqual(b["total"], 1)

    def test_home_single_request(self):
        d = self.client.get("/api/home").json()
        self.assertEqual(len(d["hero"]), 1)
        self.assertTrue(d["hero"][0]["backdrop"].endswith("b.jpg"))
        self.assertIn("w342", d["rows"][0]["results"][0]["poster"])  # smaller thumbs
        self.assertEqual(self.client.get("/api/home").headers["cache-control"].split(",")[0],
                         "public")

    def test_rate_limit_uses_forwarded_ip(self):
        # behind Vercel all users share one socket IP; limiter must key on XFF
        for i in range(130):
            r = self.client.get("/api/search", params={"q": f"a{i}"},
                                headers={"x-forwarded-for": "1.1.1.1"})
        self.assertEqual(r.status_code, 429)
        r = self.client.get("/api/search", params={"q": "zz"},
                            headers={"x-forwarded-for": "2.2.2.2"})
        self.assertEqual(r.status_code, 200)

    def test_queries_use_partial_indexes(self):
        def plan(sql, *p):
            return " ".join(r[3] for r in self.db.query("EXPLAIN QUERY PLAN " + sql, p))
        p = plan("SELECT * FROM titles WHERE tmdb_id IS NOT NULL AND poster != '' AND kind=?"
                 " ORDER BY last_file_at DESC, id ASC LIMIT 20", "movie")
        self.assertIn("idx_titles_home", p)
        self.assertNotIn("TEMP B-TREE", p)
        p = plan("SELECT * FROM titles WHERE tmdb_id IS NOT NULL AND poster != '' AND kind=?"
                 " ORDER BY rating DESC, id ASC LIMIT 20", "movie")
        self.assertIn("idx_titles_rating", p)


if __name__ == "__main__":
    unittest.main()
