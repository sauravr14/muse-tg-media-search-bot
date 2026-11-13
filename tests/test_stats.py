"""Tests for the admin usage stats panel (2026-11-13)."""
import asyncio
import calendar
import tempfile
import time
import unittest
from pathlib import Path

from app import stats as stats_mod
from app.bot import MediaBot
from app.config import Config
from app.db import DB


def _db():
    return DB(Path(tempfile.mkdtemp()) / "stats.db")


def _epoch_ist(y, mo, d, h=0, mi=0):
    """Epoch for a wall-clock time in IST (UTC+5:30)."""
    return calendar.timegm((y, mo, d, h, mi, 0)) - stats_mod.IST_OFFSET


class OverviewTest(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        # Friday 2026-11-13 20:00 IST — the "now" for every window test.
        self.now = _epoch_ist(2026, 11, 13, 20, 0)
        db = self.db
        db.execute(
            "INSERT INTO titles (id, kind, title, norm_title, orig_title,"
            " year, tmdb_id, poster, added_at, updated_at, last_file_at)"
            " VALUES (1, 'movie', 'Dune', 'dune', 'Dune', 2026, 1, 'p',"
            " 0, 0, 0), (2, 'series', 'Breaking Bad', 'breaking bad',"
            " 'Breaking Bad', 2008, 2, 'p', 0, 0, 0)")
        for fid, tid, res in ((11, 1, "1080p"), (12, 1, "720p"),
                              (21, 2, "1080p")):
            db.execute(
                "INSERT INTO files (id, title_id, file_name, norm_name,"
                " resolution, tg_chat_id, tg_msg_id, indexed_at)"
                " VALUES (?, ?, 'f.mp4', 'f', ?, -100, ?, 0)",
                (fid, tid, res, fid))
        self.db.upsert_user(10, "alice", "Alice", "", "")
        self.db.upsert_user(20, "bob", "Bob", "", "")
        # users seen today
        db.execute("UPDATE users SET first_seen=?, last_seen=?",
                   (self.now - 3600, self.now - 60))
        # one old user: first seen 10 days ago, last seen 3 days ago
        db.execute("INSERT INTO users (user_id, username, first_name,"
                   " first_seen, last_seen, msg_count) VALUES"
                   " (30, 'old', 'Old', ?, ?, 1)",
                   (self.now - 10 * 86400, self.now - 3 * 86400))
        # requests: 1 done, 2 open
        for code, st in (("RQAAAA", "done"), ("RQAAAB", "open"),
                         ("RQAAAC", "open")):
            db.execute(
                "INSERT INTO requests (code, user_id, title, status,"
                " created_at) VALUES (?, 10, 'X', ?, ?)",
                (code, st, self.now))

    def _seed_events(self):
        db, now = self.db, self.now
        ev = stats_mod.record_event
        # downloads: 2 today, 1 yesterday, 1 ten days ago, 1 forty days ago
        ev(db, "download", 10, 11, now=now - 100)
        ev(db, "download", 10, 11, now=now - 200)
        ev(db, "download", 20, 21, now=now - 86400 - 100)
        ev(db, "download", 20, 12, now=now - 10 * 86400)
        ev(db, "download", 30, 21, now=now - 40 * 86400)
        # plays: 1 today, 1 yesterday
        ev(db, "play", 10, 11, now=now - 300)
        ev(db, "play", 20, 21, now=now - 86400 - 200)
        # searches: breaking bad x3, dune x1 (mixed case/space on purpose)
        for q in ("Breaking Bad", "  breaking   BAD ", "breaking bad", "DUNE"):
            ev(db, "search", 10, detail=stats_mod.normalize_query(q),
               now=now - 400)
        # failures
        ev(db, "fsub_block", 20, now=now - 500)
        ev(db, "delivery_fail", 20, 11, detail="PeerIdInvalid",
           now=now - 600)

    def test_windows_and_totals(self):
        self._seed_events()
        o = stats_mod.overview(self.db, self.now)
        self.assertEqual(o["downloads_total"], 5)
        self.assertEqual(o["plays_total"], 2)
        self.assertEqual(o["downloads_today"], 2)
        self.assertEqual(o["plays_today"], 1)
        self.assertEqual(o["downloads_7d"], 3)
        self.assertEqual(o["plays_7d"], 2)
        self.assertEqual(o["downloads_30d"], 4)
        self.assertEqual(o["plays_30d"], 2)
        self.assertEqual(o["fsub_blocks_total"], 1)
        self.assertEqual(o["delivery_fails_total"], 1)
        self.assertEqual(o["requests_done"], 1)
        self.assertEqual(o["requests_pending"], 2)
        self.assertEqual(o["users_total"], 3)
        self.assertEqual(o["users_new_today"], 2)
        self.assertEqual(o["users_active_today"], 2)

    def test_ist_day_boundary(self):
        # 23:30 IST on the 13th counts as the 13th; 00:30 IST on the 14th
        # is the next day, not "today" for the 13th 20:00 reference.
        stats_mod.record_event(self.db, "download", 10, 11,
                               now=_epoch_ist(2026, 11, 13, 23, 30))
        stats_mod.record_event(self.db, "download", 10, 11,
                               now=_epoch_ist(2026, 11, 14, 0, 30))
        o = stats_mod.overview(self.db, self.now)
        self.assertEqual(o["downloads_today"], 1)
        self.assertEqual(o["downloads_total"], 2)

    def test_top_titles_users_searches_quality(self):
        self._seed_events()
        o = stats_mod.overview(self.db, self.now)
        # Dune has 3 downloads (2 today + 1 ten days ago), Breaking Bad 2
        self.assertEqual([(t["title"], t["c"]) for t in o["top_downloads"]],
                         [("Dune", 3), ("Breaking Bad", 2)])
        self.assertEqual([(t["title"], t["c"]) for t in o["top_plays"]],
                         [("Breaking Bad", 1), ("Dune", 1)])
        # alice (10): 2 downloads + 1 play; bob (20): 2 + 1
        self.assertEqual([u["user_id"] for u in o["top_users"]], [10, 20, 30])
        self.assertEqual(o["top_users"][0]["username"], "alice")
        self.assertEqual([(s["query"], s["c"]) for s in o["top_searches"]],
                         [("breaking bad", 3), ("dune", 1)])
        self.assertEqual({r["resolution"]: r["c"] for r in o["quality"]},
                         {"1080p": 4, "720p": 1})
        self.assertEqual(len(o["daily"]), 7)
        self.assertEqual(o["daily"][-1]["downloads"], 2)  # today
        self.assertEqual(o["daily"][-1]["plays"], 1)
        self.assertEqual(o["daily"][-2]["downloads"], 1)  # yesterday
        self.assertEqual(o["catalog"]["files"], 3)

    def test_empty_db_overview(self):
        o = stats_mod.overview(_db(), self.now)
        self.assertEqual(o["downloads_total"], 0)
        self.assertEqual(o["top_downloads"], [])
        self.assertEqual(o["top_searches"], [])
        self.assertEqual(len(o["daily"]), 7)

    def test_totals_and_normalize(self):
        self.assertEqual(stats_mod.normalize_query("  Breaking   BAD "), "breaking bad")
        self.assertEqual(stats_mod.normalize_query(""), "")
        stats_mod.record_event(self.db, "download", 10, 11, now=self.now)
        self.assertEqual(stats_mod.totals(self.db), (1, 0))


class PanelTest(unittest.TestCase):
    def test_stats_text_renders(self):
        db = _db()
        stats_mod.record_event(db, "download", 10, None, now=time.time())
        cfg = Config()
        cfg.admin_ids = (99,)
        bot = object.__new__(MediaBot)
        bot.cfg = cfg
        bot.db = db
        text = asyncio.run(bot._stats_text())
        self.assertIn("Statistics", text)
        self.assertIn("Downloads: <b>1</b>", text)
        self.assertIn("Plays: <b>0</b>", text)
        self.assertIn("Top downloaded", text)


if __name__ == "__main__":
    unittest.main()
