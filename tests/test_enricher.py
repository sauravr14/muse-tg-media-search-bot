"""Tests for the enricher backlog sweep.

Regression context (2026-10-22): the old sweep re-picked the single
OLDEST unmatched title every pass and skipped it when tried within
7 days — one unmatchable title froze the entire poster backlog
(284k titles waiting, famous titles like Death Note never attempted).
The sweep must skip tried titles IN BULK and serve most-files-first.
"""
import tempfile
import time
import unittest
from pathlib import Path

from app.classify import Classifier
from app.db import DB
from app.indexer import Indexer
from app.parser import parse_filename
from app.tmdb import TMDB


def _meta(chat, msg, name):
    return {"file_name": name, "size": 10, "duration": 5,
            "mime": "video/x-matroska", "tg_chat_id": chat,
            "tg_msg_id": msg, "caption": "", "msg_date": 0}


class BacklogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = DB(Path(tempfile.mkdtemp()) / "enrich.db")
        clf = Classifier(cls.db)
        # Big Buck Bunny: 3 files. Dark: 1 file. Obscure Junk: 2 files.
        for i in range(3):
            clf.ingest(parse_filename(f"Big Buck Bunny 2008 {480 + i}p.mkv"),
                       _meta(-1, 100 + i, "bbb.mkv"))
        clf.ingest(parse_filename("Dark S01E01 720p.mkv"),
                   _meta(-1, 200, "dark.mkv"))
        for i in range(2):
            clf.ingest(parse_filename(f"Obscure Junk 1999 {i}.mkv"),
                       _meta(-1, 300 + i, "junk.mkv"))
        cls.idx = Indexer(None, cls.db, TMDB(cls.db, ""))
        rows = {r["title"]: r["id"] for r in
                cls.db.query("SELECT id, title FROM titles")}
        cls.bbb, cls.dark, cls.junk = (rows["Big Buck Bunny"], rows["Dark"],
                                       rows["Obscure Junk"])

    def test_most_files_first(self):
        ids = self.idx._backlog_ids(10)
        self.assertEqual(ids, [self.bbb, self.junk, self.dark])

    def test_matched_titles_excluded(self):
        self.db.execute("UPDATE titles SET tmdb_id=123 WHERE id=?", (self.bbb,))
        try:
            self.assertNotIn(self.bbb, self.idx._backlog_ids(10))
        finally:
            self.db.execute("UPDATE titles SET tmdb_id=NULL WHERE id=?", (self.bbb,))

    def test_recently_tried_excluded_but_stale_included(self):
        self.db.set_kv(f"tmdb_try:{self.junk}", str(time.time()))
        self.db.set_kv(f"tmdb_try:{self.dark}",
                       str(time.time() - 8 * 24 * 3600))
        ids = self.idx._backlog_ids(10)
        self.assertNotIn(self.junk, ids)   # tried today: skipped in bulk
        self.assertIn(self.dark, ids)      # tried 8 days ago: eligible again
        self.assertEqual(ids[0], self.bbb)

    def test_limit_respected(self):
        self.assertEqual(len(self.idx._backlog_ids(2)), 2)


if __name__ == "__main__":
    unittest.main()
