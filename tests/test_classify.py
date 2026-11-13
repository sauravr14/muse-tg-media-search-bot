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

    def test_range_pack_creates_episode_rows(self):
        # a range pack files under the season AND creates a row for every
        # covered episode, so the season does not look half-scanned
        name = "You (2018) S01 E01-05 480p HEVC HDRip.mkv"
        p = parse_filename(name)
        r = self.clf.ingest(p, _file_meta(-1001, 77, name))
        f = self.db.query_one("SELECT * FROM files WHERE id=?", (r["file_id"],))
        self.assertIsNone(f["episode_id"])
        self.assertIsNotNone(f["season_id"])
        self.assertEqual(f["pack_label"], "E01\u2013E05")
        eps = self.db.query(
            "SELECT ep_no FROM episodes WHERE season_id=? ORDER BY ep_no",
            (f["season_id"],))
        self.assertEqual([e["ep_no"] for e in eps], [1, 2, 3, 4, 5])

    def test_episode_file_records_season(self):
        name = "Breaking.Bad.S01E02.720p.mkv"
        p = parse_filename(name)
        r = self.clf.ingest(p, _file_meta(-1001, 78, name))
        f = self.db.query_one("SELECT * FROM files WHERE id=?", (r["file_id"],))
        self.assertIsNotNone(f["episode_id"])
        self.assertIsNotNone(f["season_id"])

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

    def test_same_name_different_year_splits(self):
        """Regression: The Intern 2015 vs the 2026 Korean remake.
        When only the 2015 title exists, a 2026 file must NOT attach to
        it (the old single-row shortcut ignored the year) — it gets its
        own 2026 title, and a second 2026 file joins that title."""
        r15 = self.clf.ingest(parse_filename("The.Intern.2015.720p.mkv"),
                              _file_meta(-1001, 10, "The.Intern.2015.720p.mkv"))
        r26 = self.clf.ingest(parse_filename("The.Intern.2026.720p.WEB-DL.x264-Pahe.in.mkv"),
                              _file_meta(-1001, 11, "The.Intern.2026.720p.WEB-DL.x264-Pahe.in.mkv"))
        self.assertNotEqual(r26["title_id"], r15["title_id"])
        row26 = self.db.query_one("SELECT year FROM titles WHERE id=?", (r26["title_id"],))
        self.assertEqual(row26["year"], 2026)
        row15 = self.db.query_one("SELECT year FROM titles WHERE id=?", (r15["title_id"],))
        self.assertEqual(row15["year"], 2015)
        # second 2026 file joins the 2026 title, not a third one
        r26b = self.clf.ingest(parse_filename("The.Intern.2026.1080p.WEB-DL.x264-Pahe.in.mkv"),
                               _file_meta(-1001, 12, "The.Intern.2026.1080p.WEB-DL.x264-Pahe.in.mkv"))
        self.assertEqual(r26b["title_id"], r26["title_id"])
        n = self.db.query_one("SELECT COUNT(*) c FROM titles WHERE norm_title='the intern'")
        self.assertEqual(n["c"], 2)

    def test_same_name_same_year_still_groups(self):
        # the fix must not split the normal case: same name + same year
        ids = set()
        for i, name in enumerate(["The.Intern.2015.720p.mkv",
                                  "The.Intern.2015.1080p.BluRay.mkv"]):
            r = self.clf.ingest(parse_filename(name), _file_meta(-1001, 20 + i, name))
            ids.add(r["title_id"])
        self.assertEqual(len(ids), 1)

    def test_yearless_title_still_matches_year_known_file(self):
        """A title with no year has nothing to contradict, so it still
        matches — same rule as the FTS fallback (year in (p.year, None))."""
        r0 = self.clf.ingest(parse_filename("The.Intern.720p.mkv"),
                             _file_meta(-1001, 30, "The.Intern.720p.mkv"))
        row0 = self.db.query_one("SELECT year FROM titles WHERE id=?", (r0["title_id"],))
        self.assertIsNone(row0["year"])
        r26 = self.clf.ingest(parse_filename("The.Intern.2026.720p.mkv"),
                              _file_meta(-1001, 31, "The.Intern.2026.720p.mkv"))
        self.assertEqual(r26["title_id"], r0["title_id"])


if __name__ == "__main__":
    unittest.main()
