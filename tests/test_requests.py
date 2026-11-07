"""Tests for the /request flow (2026-11-07): store + 5/day IST cap +
#requested log + /done close-out, fresh force-sub checks, and the
Mini App Request button wiring.

Bot-handler behaviour is guarded at source level (same pattern as
test_dispatcher_shadowing.py): constructing a full Bot needs a live
Pyrogram Client, so the dispatch order and filter guards are asserted
on bot.py text, while the store/cap/log logic is tested directly.
"""
import asyncio
import calendar
import re
import tempfile
import unittest
from pathlib import Path

from app import owner as owner_mod
from app import requests as req_mod
from app.db import DB

APP_DIR = Path(__file__).resolve().parent.parent / "app"
BOT_PY = APP_DIR / "bot.py"


def _db():
    return DB(Path(tempfile.mkdtemp()) / "req.db")


def _ts(y, mo, d, h=0, mi=0):
    """UTC epoch for a UTC date/time (tests build IST times from this)."""
    return calendar.timegm((y, mo, d, h, mi, 0, 0, 0, 0))


# 2026-11-07 10:00 IST == 2026-11-07 04:30 UTC; next day same time.
DAY1 = _ts(2026, 11, 7, 4, 30)
DAY2 = _ts(2026, 11, 8, 4, 30)


class RequestStoreTest(unittest.TestCase):
    def setUp(self):
        self.db = _db()

    def test_code_shape_and_uniqueness(self):
        codes = {req_mod.generate_code() for _ in range(2000)}
        self.assertEqual(len(codes), 2000)
        for c in codes:
            self.assertEqual(len(c), req_mod.CODE_LEN)
            self.assertTrue(set(c) <= set(req_mod.CODE_ALPHABET))
            # No ambiguous chars to misread off a log line.
            self.assertFalse(set(c) & set("01IL"))

    def test_day_start_ist(self):
        # 10:00 IST -> that day's 00:00 IST == 18:30 UTC previous day.
        self.assertEqual(req_mod.day_start_ist(DAY1), _ts(2026, 11, 6, 18, 30))
        # 00:30 IST is still the same IST day.
        self.assertEqual(req_mod.day_start_ist(_ts(2026, 11, 6, 19, 0)),
                         _ts(2026, 11, 6, 18, 30))

    def test_create_and_fetch(self):
        row = req_mod.create_request(self.db, 111, "alice", "Alice",
                                     "Avengers Endgame", now=DAY1)
        self.assertEqual(row["user_id"], 111)
        self.assertEqual(row["username"], "alice")
        self.assertEqual(row["title"], "Avengers Endgame")
        self.assertEqual(row["status"], "open")
        self.assertEqual(req_mod.get_request_by_code(self.db, row["code"]),
                         row)
        self.assertEqual(req_mod.get_request_by_code(self.db, "ZZZZZZ"), None)
        self.assertEqual(req_mod.get_request_by_code(
            self.db, row["code"].lower())["code"], row["code"])

    def test_title_trimmed_and_capped(self):
        row = req_mod.create_request(self.db, 111, "", "Bob",
                                     "  Dune  ", now=DAY1)
        self.assertEqual(row["title"], "Dune")
        long_row = req_mod.create_request(self.db, 112, "", "Bob",
                                          "x" * 500, now=DAY1)
        self.assertEqual(len(long_row["title"]), 200)
        with self.assertRaises(ValueError):
            req_mod.create_request(self.db, 113, "", "Bob", "   ", now=DAY1)

    def test_five_per_day_cap_and_next_day_reset(self):
        for i in range(req_mod.REQUESTS_PER_DAY):
            req_mod.create_request(self.db, 111, "alice", "Alice",
                                   f"Movie {i}", now=DAY1)
        self.assertEqual(req_mod.count_requests_today(self.db, 111, DAY1), 5)
        with self.assertRaises(req_mod.DailyCapReached):
            req_mod.create_request(self.db, 111, "alice", "Alice",
                                   "Movie 6", now=DAY1)
        # Another user is unaffected.
        req_mod.create_request(self.db, 222, "bob", "Bob", "Movie 0",
                               now=DAY1)
        # Next IST day the cap resets for the same user.
        row = req_mod.create_request(self.db, 111, "alice", "Alice",
                                     "Movie next day", now=DAY2)
        self.assertEqual(row["status"], "open")
        self.assertEqual(req_mod.count_requests_today(self.db, 111, DAY2), 1)

    def test_mark_done_flow(self):
        row = req_mod.create_request(self.db, 111, "alice", "Alice",
                                     "Dune", now=DAY1)
        self.assertIsNone(req_mod.mark_request_done(self.db, "NOPE12"))
        done = req_mod.mark_request_done(self.db, row["code"], now=DAY1 + 60)
        self.assertEqual(done["status"], "done")
        self.assertFalse(done["already_done"])
        self.assertEqual(done["title"], "Dune")
        again = req_mod.mark_request_done(self.db, row["code"])
        self.assertTrue(again["already_done"])
        self.assertEqual(again["status"], "done")


class RequestLogTest(unittest.TestCase):
    def test_format_request_log(self):
        msg = owner_mod.format_request_log(0, 111, "alice", "Alice",
                                           "Dune <Part Two>", "RQ7K2P")
        self.assertIn("#requested", msg)
        self.assertIn("Dune &lt;Part Two&gt;", msg)
        self.assertIn("RQ7K2P", msg)
        self.assertIn("/done RQ7K2P", msg)
        # The whole /done command is one tap-to-copy code span, so the
        # admin can paste it straight into the bot DM.
        self.assertIn("<code>/done RQ7K2P</code>", msg)
        self.assertIn("@alice", msg)
        self.assertIn("111", msg)


class FakeMember:
    def __init__(self, status):
        self.status = status


class FakeClient:
    def __init__(self, statuses):
        self.statuses = statuses

    async def get_chat_member(self, chat_id, user_id):
        s = self.statuses.get(chat_id, "member")
        if s == "not_participant":
            raise type("UserNotParticipant", (Exception,), {})("nope")
        return FakeMember(s)


class FreshFsubTest(unittest.TestCase):
    """The request flow must never trust the cached 'joined' verdict:
    fsub_forget + a live check is what _unjoined_fresh does."""

    def test_forget_forces_live_recheck(self):
        async def run():
            entries = [{"chat_id": -1001, "title": "Ch1"}]
            client = FakeClient({-1001: "member"})
            uid = 999001
            owner_mod.fsub_forget(uid)
            self.assertEqual(
                await owner_mod.unjoined_channels(client, uid, entries), [])
            # User leaves: cached verdict still says joined.
            client.statuses[-1001] = "left"
            self.assertEqual(
                await owner_mod.unjoined_channels(client, uid, entries), [])
            # Forgetting forces the live check — now unjoined.
            owner_mod.fsub_forget(uid)
            self.assertEqual(
                await owner_mod.unjoined_channels(client, uid, entries),
                entries)
            owner_mod.fsub_forget(uid)

        asyncio.run(run())


def _fn_body(src: str, name: str) -> str:
    m = re.search(rf"async def {name}\b(.*?)(?=\n        @c\.|\n    def |\Z)",
                  src, re.S)
    assert m, f"{name} not found in bot.py"
    return m.group(1)


class RequestWiringTest(unittest.TestCase):
    """Source-level guards for the dispatcher pieces (Bot needs a live
    Pyrogram Client, so these assert on bot.py text)."""

    def setUp(self):
        self.src = BOT_PY.read_text()

    def test_request_and_done_handlers_exist(self):
        self.assertIn('filters.command("request")', self.src)
        self.assertIn('filters.command("done") & self._owner_filter()',
                      self.src)
        self.assertIn('arg[1] == "request"', self.src)

    def test_pending_request_before_plain_search(self):
        # The capture handler must be registered before plain_search,
        # or the title would be searched instead of stored.
        self.assertLess(self.src.index("async def pending_request"),
                        self.src.index("async def plain_search"))
        body = _fn_body(self.src, "pending_request")
        self.assertIn("raise ContinuePropagation", body)
        self.assertIn("raise StopPropagation", body)
        self.assertIn('st.get("kind") != "request"', body)

    def test_pending_owner_yields_request_mode(self):
        body = _fn_body(self.src, "pending_owner")
        self.assertIn('st.get("kind") == "request"', body)

    def test_submit_uses_fresh_fsub(self):
        for name in ("_submit_request", "_start_request"):
            self.assertIn("_unjoined_fresh", _fn_body(self.src, name))

    def test_help_mentions_request(self):
        self.assertIn("/request <name>", self.src)

    def test_frontend_button_wiring(self):
        html = (APP_DIR / "webapp" / "index.html").read_text()
        js = (APP_DIR / "webapp" / "app.js").read_text()
        css = (APP_DIR / "webapp" / "styles.css").read_text()
        self.assertIn('id="reqBtn"', html)
        # Button sits in its own row under the Clean/All toggle row,
        # above the search bar.
        self.assertLess(html.index("cam-row"), html.index("req-row"))
        self.assertLess(html.index("req-row"), html.index("search-wrap"))
        self.assertIn("t.me/CORETGOFFICIALBOT?start=request", js)
        self.assertIn("openTelegramLink", js)
        # Tapping Request must minimise the Mini App (tg.close) so the
        # user lands in the bot DM and sees the flow start.
        self.assertIn("tg.close()", js)
        self.assertIn(".req-btn", css)
        # Clean/All toggle: a real switch — All prints left (default),
        # Clean prints right, knob slides; both labels printed in small
        # type, active one bright. No more single label that flips.
        self.assertIn('id="camToggle"', html)
        self.assertIn("All prints", html)
        self.assertIn("Clean prints", html)
        self.assertIn("cam-knob", html)
        self.assertIn(".cam-switch", css)
        self.assertIn("aria-checked", js)


if __name__ == "__main__":
    unittest.main()
