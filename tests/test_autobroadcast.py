"""Tests for the scheduled auto-broadcast (2026-11-13)."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from pyrogram.enums import ParseMode

from app import autobroadcast as autobc_mod
from app.bot import MediaBot
from app.config import Config
from app.db import DB
from app.llm import LLTitle
from tests.test_llm import FakeSession


def _db():
    return DB(Path(tempfile.mkdtemp()) / "autobc.db")


def _seed_title(db, tid, kind, title, year, popularity, poster,
                last_file_hours_ago, rating=None):
    now = time.time()
    db.execute(
        "INSERT INTO titles (id, kind, title, norm_title, orig_title, year,"
        " tmdb_id, poster, rating, popularity, added_at, updated_at,"
        " last_file_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, kind, title, title.lower(), title, year, 1000 + tid,
         poster, rating, popularity, now, now,
         now - last_file_hours_ago * 3600))


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.db = _db()

    def test_defaults_are_on_12h_everyone(self):
        st = autobc_mod.get_settings(self.db)
        self.assertTrue(st["enabled"])
        self.assertEqual(st["interval_hours"], 12)
        self.assertEqual(st["scope"], "all")
        self.assertEqual(st["last_post_at"], 0.0)
        self.assertIsNone(st["last_title_id"])

    def test_round_trip_and_bad_json(self):
        autobc_mod.set_settings(self.db, {"enabled": False,
                                          "interval_hours": 6,
                                          "scope": "users"})
        st = autobc_mod.get_settings(self.db)
        self.assertFalse(st["enabled"])
        self.assertEqual(st["interval_hours"], 6)
        self.assertEqual(st["scope"], "users")
        self.db.set_kv(autobc_mod.KV_KEY, "{not json")
        self.assertTrue(autobc_mod.get_settings(self.db)["enabled"])

    def test_next_interval_cycles(self):
        self.assertEqual(autobc_mod.next_interval(6), 12)
        self.assertEqual(autobc_mod.next_interval(12), 24)
        self.assertEqual(autobc_mod.next_interval(24), 6)
        self.assertEqual(autobc_mod.next_interval(99), 12)


class PickerTest(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        now = time.time()
        self.now = now
        # 2026 movie, most popular -> first pick
        _seed_title(self.db, 1, "movie", "Spider-Man: Brand New Day", 2026,
                    1200.0, "https://img/spidey.jpg", 130)
        # 2026 series, fresh, less popular -> second
        _seed_title(self.db, 2, "series", "VisionQuest", 2026, 370.0,
                    "https://img/vq.jpg", 2)
        # 2005 evergreen, huge popularity but old year -> after 2026s
        _seed_title(self.db, 3, "series", "Grey's Anatomy", 2005, 6300.0,
                    "https://img/greys.jpg", 35)
        # no poster -> never picked
        _seed_title(self.db, 4, "movie", "No Poster Film", 2026, 9999.0,
                    "", 5)
        # stale (>7d) -> never picked
        _seed_title(self.db, 5, "movie", "Stale Hit", 2026, 8888.0,
                    "https://img/stale.jpg", 200)

    def test_picks_recent_year_then_popularity(self):
        row = autobc_mod.pick_title(self.db, self.now)
        self.assertEqual(row["id"], 1)
        autobc_mod.record_post(self.db, 1, self.now)
        self.assertEqual(autobc_mod.pick_title(self.db, self.now)["id"], 2)

    def test_old_evergreen_after_recent_years(self):
        autobc_mod.record_post(self.db, 1, self.now)
        autobc_mod.record_post(self.db, 2, self.now)
        self.assertEqual(autobc_mod.pick_title(self.db, self.now)["id"], 3)

    def test_never_repeats_and_skips_dry_pool(self):
        for tid in (1, 2, 3):
            autobc_mod.record_post(self.db, tid, self.now)
        self.assertIsNone(autobc_mod.pick_title(self.db, self.now))
        self.assertTrue(autobc_mod.was_posted(self.db, 2))
        self.assertFalse(autobc_mod.was_posted(self.db, 4))


class CaptionTest(unittest.TestCase):
    def test_movie_caption_bold_title(self):
        c = autobc_mod.build_caption(
            {"title": "The Odyssey", "year": 2026, "kind": "movie",
             "rating": 8.0})
        self.assertIn("<b>The Odyssey</b> (2026)", c)
        self.assertIn("Movie", c)
        self.assertIn("8.0/10", c)
        self.assertIn("now available on the bot", c)

    def test_series_caption_no_rating(self):
        c = autobc_mod.build_caption(
            {"title": "Carrie", "year": 2026, "kind": "series",
             "rating": None})
        self.assertIn("TV show", c)
        self.assertIn("<b>Carrie</b> (2026)", c)
        self.assertNotIn("⭐", c)

    def test_title_is_escaped(self):
        c = autobc_mod.build_caption(
            {"title": "A <b>Fake</b> & Co", "year": None, "kind": "movie",
             "rating": None})
        self.assertIn("&lt;b&gt;Fake&lt;/b&gt; &amp; Co", c)
        self.assertNotIn("<b>Fake</b>", c)


class LLMcaptionTest(unittest.TestCase):
    def test_caption_returns_line(self):
        payload = {"choices": [{"message": {"content":
            "🔥 Watch trending Movie <b>Dune</b> (2026) — now on the bot!"}}]}
        llm = LLTitle("test-key", session=FakeSession(payload))
        out = asyncio.run(llm.caption("Dune", 2026, "movie", 8.5))
        self.assertIn("<b>Dune</b>", out)

    def test_caption_rate_limit_returns_none(self):
        llm = LLTitle("test-key", session=FakeSession({}, status=429))
        self.assertIsNone(asyncio.run(llm.caption("Dune", 2026, "movie", 8.5)))

    def test_caption_no_key_returns_none(self):
        llm = LLTitle("")
        self.assertIsNone(asyncio.run(llm.caption("Dune", 2026, "movie", 8.5)))


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text,
                          "parse_mode": kwargs.get("parse_mode"),
                          "reply_markup": kwargs.get("reply_markup")})

    async def send_photo(self, chat_id, photo, **kwargs):
        self.sent.append({"chat_id": chat_id, "photo": photo,
                          "caption": kwargs.get("caption"),
                          "parse_mode": kwargs.get("parse_mode"),
                          "reply_markup": kwargs.get("reply_markup")})


class TickTest(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        _seed_title(self.db, 1, "movie", "Spider-Man: Brand New Day", 2026,
                    1200.0, "https://img/spidey.jpg", 130, rating=8.4)
        _seed_title(self.db, 2, "series", "VisionQuest", 2026, 370.0,
                    "https://img/vq.jpg", 2, rating=8.7)
        cfg = Config()
        cfg.mini_app_url = "https://muse-tg-media-search-bot-webapp.vercel.app/"
        cfg.admin_ids = (99,)
        self.fake = FakeClient()
        bot = object.__new__(MediaBot)
        bot.cfg = cfg
        bot.db = self.db
        bot.client = self.fake
        bot.llm = None
        bot._sudo_ids = set()
        self.bot = bot
        self.db.upsert_user(10, "a", "A", "", "")
        self.db.upsert_source(-400, "Src", "")

    def test_tick_posts_once_then_not_due(self):
        res = asyncio.run(self.bot._autobc_tick())
        self.assertIsNotNone(res)
        self.assertEqual(res["delivered"], 2)
        photos = [s for s in self.fake.sent if "photo" in s]
        self.assertEqual(len(photos), 2)
        self.assertEqual(photos[0]["photo"], "https://img/spidey.jpg")
        self.assertEqual(photos[0]["parse_mode"], ParseMode.HTML)
        self.assertIn("<b>Spider-Man: Brand New Day</b>", photos[0]["caption"])
        # Mini App button on every post (web_app for the user, link for chat)
        btn = photos[0]["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(btn.text, "🌐 Open Media Library")
        self.assertIsNotNone(btn.web_app)
        btn2 = photos[1]["reply_markup"].inline_keyboard[0][0]
        self.assertIsNone(btn2.web_app)
        # recorded + timer set, so the next tick is not due
        self.assertTrue(autobc_mod.was_posted(self.db, 1))
        self.assertIsNone(asyncio.run(self.bot._autobc_tick()))
        self.assertEqual(len(self.fake.sent), 3)  # 2 photos + owner report

    def test_tick_skips_when_disabled(self):
        autobc_mod.set_settings(self.db, {"enabled": False})
        self.assertIsNone(asyncio.run(self.bot._autobc_tick()))
        self.assertEqual(self.fake.sent, [])

    def test_force_posts_when_disabled(self):
        autobc_mod.set_settings(self.db, {"enabled": False})
        res = asyncio.run(self.bot._autobc_tick(force=True))
        self.assertIsNotNone(res)
        self.assertEqual(res["delivered"], 2)

    def test_tick_skips_dry_pool(self):
        autobc_mod.record_post(self.db, 1, time.time())
        autobc_mod.record_post(self.db, 2, time.time())
        self.assertIsNone(asyncio.run(self.bot._autobc_tick()))
        self.assertEqual(self.fake.sent, [])

    def test_ai_caption_fallback_to_template(self):
        payload = {"choices": [{"message": {"content": "plain line, no bold"}}]}
        self.bot.llm = LLTitle("test-key", session=FakeSession(payload))
        c = asyncio.run(self.bot._autobc_caption(
            {"title": "Dune", "year": 2026, "kind": "movie", "rating": 8.5}))
        self.assertIn("<b>Dune</b>", c)


if __name__ == "__main__":
    unittest.main()
