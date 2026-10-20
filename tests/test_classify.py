"""Tests for classifier grouping: one movie card, series season/episode trees."""
import tempfile
import unittest
from pathlib import Path

from app.classify import Classifier
from app.db import DB
from app.parser import parse_filename


def _file_meta(chat, msg, name="f.mkv"):
    return {"file_name": name, "size": 100, "duration": 60, "mime": "video/x-matroska",
            "tg_chat_id": chat, "tg_msg_id": msg, "caption": "", "msg_date": 0}


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.db = DB(Path(tempfile.mkdtemp()) / "t.db")
        self.clf = Classifier(self.db)

    def test_movie_versions_grouped(self):
        ids = set()
        for i, res in enumerate(["480p", "720p", "1080p"]):
            p = parse_filename(f"Inception.2010.{res}.mkv")
            r = self.clf.ingest(p, _file_meta(-1001, 10 + i, f"Inception.2010.{res}.mkv"))
            ids.add(r["title_id"])
        self.assertEqual(len(ids), 1)  # one canonical movie
        n = self.db.query_one("SELECT COUNT(*) c FROM files WHERE title_id=?", (next(iter(ids)),))
        self.assertEqual(n["c"], 3)
        movies = self.db.query_one("SELECT COUNT(*) c FROM titles WHERE kind='movie'")
        self.assertEqual(movies["c"], 1)

    def test_series_grouping(self):
        files = [
            ("Breaking.Bad.S01E01.720p.mkv", 1), ("Breaking.Bad.S01E01.1080p.mkv", 2),
            ("Breaking.Bad.S01E02.720p.mkv", 3), ("Breaking.Bad.S02E01.720p.mkv", 4),
        ]
        title_ids = set()
        for name, msg in files:
            p = parse_filename(name)
            r = self.clf.ingest(p, _file_meta(-1001, msg, name))
            title_ids.add(r["title_id"])
        self.assertEqual(len(title_ids), 1)
        seasons = self.db.query("SELECT season_no FROM seasons ORDER BY season_no")
        self.assertEqual([s["season_no"] for s in seasons], [1, 2])
        eps = self.db.query(
            "SELECT e.ep_no, s.season_no FROM episodes e JOIN seasons s ON s.id=e.season_id "
            "ORDER BY s.season_no, e.ep_no")
        self.assertEqual([(e["season_no"], e["ep_no"]) for e in eps], [(1, 1), (1, 2), (2, 1)])
        # episode 1 has both resolutions
        ep1 = self.db.query_one("SELECT id FROM episodes WHERE ep_no=1 AND season_id="
                                "(SELECT id FROM seasons WHERE season_no=1)")
        res = self.db.query("SELECT resolution FROM files WHERE episode_id=?", (ep1["id"],))
        self.assertEqual(sorted(r["resolution"] for r in res), ["1080p", "720p"])

    def test_idempotent(self):
        p = parse_filename("Dune.2021.720p.mkv")
        r1 = self.clf.ingest(p, _file_meta(-1001, 50, "Dune.2021.720p.mkv"))
        r2 = self.clf.ingest(p, _file_meta(-1001, 50, "Dune.2021.720p.mkv"))
        self.assertTrue(r2["duplicate"])
        self.assertEqual(r1["file_id"], r2["file_id"])

    def test_fts_fallback_never_captures_onto_junk_title(self):
        """A leftover junk title whose norm merely STARTS WITH the clean
        title ("vivaha bhojanambu 700mb") must not recapture clean files
        via the FTS phrase fallback — the file gets its own clean title."""
        junk = parse_filename("Vivaha.Bhojanambu.2021.720p.mkv")
        junk.title = "Vivaha Bhojanambu 700Mb"
        junk.norm_title = "vivaha bhojanambu 700mb"
        junk_row = self.clf.create_title(junk, None)
        clean = parse_filename("Vivaha.Bhojanambu.2021.720p.mkv")
        self.assertIsNone(self.clf.find_title(clean))
        r = self.clf.ingest(clean, _file_meta(-1001, 99,
                                              "Vivaha.Bhojanambu.2021.720p.mkv"))
        self.assertNotEqual(r["title_id"], junk_row["id"])
        row = self.db.query_one("SELECT norm_title FROM titles WHERE id=?",
                                (r["title_id"],))
        self.assertEqual(row["norm_title"], "vivaha bhojanambu")

    def test_duplicate_sources_kept(self):
        # same movie in two channels -> one title, two file rows
        for chat in (-1001, -1002):
            p = parse_filename("Interstellar.2014.1080p.mkv")
            self.clf.ingest(p, _file_meta(chat, 7, "Interstellar.2014.1080p.mkv"))
        n = self.db.query_one("SELECT COUNT(*) c FROM titles")
        self.assertEqual(n["c"], 1)
        f = self.db.query_one("SELECT COUNT(*) c FROM files")
        self.assertEqual(f["c"], 2)


if __name__ == "__main__":
    unittest.main()
