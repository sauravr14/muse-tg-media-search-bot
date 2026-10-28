"""Tests for owner features: settings/fsub/users/broadcast/live logs."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from pyrogram.types import InlineKeyboardButton

from fastapi.testclient import TestClient

from app import owner as owner_mod
from app.api import create_app
from app.config import Config
from app.db import DB


class FakeMember:
    def __init__(self, status):
        self.status = status


class FakeClient:
    """status map: chat_id -> status string; 'error' raises a generic error."""
    def __init__(self, statuses):
        self.statuses = statuses

    async def get_chat_member(self, chat_id, user_id):
        s = self.statuses.get(chat_id, "member")
        if s == "error":
            raise RuntimeError("boom")
        if s == "not_participant":
            raise type("UserNotParticipant", (Exception,), {})("nope")
        return FakeMember(s)


def _db():
    return DB(Path(tempfile.mkdtemp()) / "owner.db")


class OwnerTest(unittest.TestCase):
    def setUp(self):
        self.db = _db()

    # -- settings kv ---------------------------------------------------------
    def test_start_photo_and_text_round_trip(self):
        self.assertEqual(owner_mod.get_start_photo(self.db), "")
        owner_mod.set_start_photo(self.db, "FILE123")
        self.assertEqual(owner_mod.get_start_photo(self.db), "FILE123")
        owner_mod.set_start_photo(self.db, "")
        self.assertEqual(owner_mod.get_start_photo(self.db), "")
        self.assertIn("Core TG", owner_mod.get_start_text(self.db))
        owner_mod.set_start_text(self.db, "Hello")
        self.assertEqual(owner_mod.get_start_text(self.db), "Hello")

    def test_log_channel_resolution(self):
        # env fallback
        self.assertEqual(owner_mod.resolve_log_channel(self.db, -1001), -1001)
        # kv wins over env
        owner_mod.set_log_channel(self.db, -1002)
        self.assertEqual(owner_mod.resolve_log_channel(self.db, -1001), -1002)
        # kv '0' disables even when env is set
        self.db.set_kv(owner_mod.KV_LOG_CHANNEL, "0")
        self.assertEqual(owner_mod.resolve_log_channel(self.db, -1001), 0)

    # -- fsub ----------------------------------------------------------------
    def test_fsub_json_round_trip(self):
        entries = [{"chat_id": -1001, "title": "Ch1", "username": "ch1",
                    "invite_link": "https://t.me/+abc"},
                   {"chat_id": -1002, "title": "Ch2", "username": "",
                    "invite_link": ""}]
        owner_mod.set_fsub(self.db, entries)
        self.assertEqual(owner_mod.fsub_list(self.db), entries)
        self.assertEqual(owner_mod.fsub_invite(entries[0]), "https://t.me/+abc")
        self.assertEqual(owner_mod.fsub_invite(entries[1]), "")

    def test_fsub_invite_username_fallback(self):
        self.assertEqual(
            owner_mod.fsub_invite({"chat_id": -1, "username": "chan"}),
            "https://t.me/chan")

    def test_unjoined_list_with_fake_client(self):
        entries = [{"chat_id": -1, "title": "A"},
                   {"chat_id": -2, "title": "B"},
                   {"chat_id": -3, "title": "C"},
                   {"chat_id": -4, "title": "D"}]
        client = FakeClient({-1: "member", -2: "left",
                             -3: "not_participant", -4: "error"})
        got = asyncio.run(owner_mod.unjoined_channels(client, 42, entries))
        self.assertEqual([e["chat_id"] for e in got], [-2, -3])
        # privileged users bypass entirely
        got = asyncio.run(owner_mod.unjoined_channels(client, 42, entries,
                                                      is_privileged=True))
        self.assertEqual(got, [])
        # empty fsub -> nobody blocked
        got = asyncio.run(owner_mod.unjoined_channels(client, 42, []))
        self.assertEqual(got, [])

    def test_parse_chat_arg(self):
        self.assertEqual(owner_mod.parse_chat_arg("@chan"), "chan")
        self.assertEqual(owner_mod.parse_chat_arg("-100123"), -100123)
        self.assertIsNone(owner_mod.parse_chat_arg("not a chat!!"))
        self.assertIsNone(owner_mod.parse_chat_arg(""))

    # -- users -----------------------------------------------------------------
    def test_users_upsert_and_pagination(self):
        for uid in range(1, 31):
            self.db.upsert_user(uid, f"u{uid}", f"First{uid}", "", f"msg {uid}")
        self.assertEqual(self.db.count_users(), 30)
        page1 = self.db.list_users(25, 0)
        page2 = self.db.list_users(25, 25)
        self.assertEqual(len(page1), 25)
        self.assertEqual(len(page2), 5)
        ids = {r["user_id"] for r in page1} | {r["user_id"] for r in page2}
        self.assertEqual(ids, set(range(1, 31)))
        # second upsert bumps msg_count, keeps first_seen, updates last_text
        row0 = self.db.query_one("SELECT * FROM users WHERE user_id=1")
        self.db.upsert_user(1, "u1", "First1", "", "second")
        row1 = self.db.query_one("SELECT * FROM users WHERE user_id=1")
        self.assertEqual(row1["msg_count"], row0["msg_count"] + 1)
        self.assertEqual(row1["first_seen"], row0["first_seen"])
        self.assertEqual(row1["last_text"], "second")

    def test_users_empty_state(self):
        self.assertEqual(self.db.count_users(), 0)
        self.assertEqual(self.db.list_users(25, 0), [])

    # -- broadcast recipients ----------------------------------------------------
    def test_broadcast_recipients_dedup_and_log_exclusion(self):
        log_ch = -100999
        self.db.upsert_user(10, "a", "A", "", "")
        self.db.upsert_user(20, "b", "B", "", "")
        self.db.upsert_chat(-200, "group", "G", "")
        self.db.upsert_chat(-300, "channel", "C", "")
        self.db.upsert_source(-300, "C", "")
        self.db.upsert_source(-400, "Src", "")
        # log channel must never receive a broadcast, even if recorded
        self.db.upsert_user(log_ch, "log", "L", "", "")
        got = owner_mod.collect_broadcast_recipients(self.db, log_ch)
        self.assertEqual(got, [10, 20, -300, -200, -400])
        self.assertNotIn(log_ch, got)
        self.assertEqual(len(got), len(set(got)))

    # -- live logs ---------------------------------------------------------------
    def test_log_truncation_and_admin_exclusion(self):
        long_text = "x" * 500
        msg = owner_mod.format_log_message(0, 42, "bob", "Bob", long_text)
        # 300 chars of body, not 500; user id present
        self.assertIn("x" * 300, msg)
        self.assertNotIn("x" * 301, msg)
        self.assertIn("<code>42</code>", msg)
        self.assertTrue(owner_mod.should_log_user(42, False))
        self.assertFalse(owner_mod.should_log_user(42, True))
        self.assertFalse(owner_mod.should_log_user(0, False))

    # -- start card ---------------------------------------------------------------
    def test_start_card_button_visibility(self):
        def app_btn(text):
            return InlineKeyboardButton(text, url="https://example.com/app/")
        owner_kb = owner_mod.build_start_keyboard(app_btn, True)
        user_kb = owner_mod.build_start_keyboard(app_btn, False)
        owner_texts = [b.text for row in owner_kb.inline_keyboard for b in row]
        user_texts = [b.text for row in user_kb.inline_keyboard for b in row]
        self.assertIn("⚙️ Settings", owner_texts)
        self.assertNotIn("⚙️ Settings", user_texts)
        self.assertIn("🌐 Open Media Library", user_texts)
        self.assertIn("ℹ️ Help", user_texts)
        self.assertIn("😊 About", user_texts)

    # -- API force-subscribe gate ------------------------------------------------
    def test_api_catalog_gated_only_when_fsub_set(self):
        class StubBot:
            client = object()

            async def unjoined_fsub(self, user_id):
                return [{"chat_id": -1001, "title": "Ch1",
                         "username": "ch1", "invite_link": ""}]

            async def deliver_to_user(self, user_id, file_id):
                return True, "sent"

        cfg = Config()
        cfg.bot_token = "test-token"
        # fsub empty -> anonymous catalog works exactly as before
        client = TestClient(create_app(cfg, self.db, bot_username="t",
                                       bot=StubBot()))
        self.assertEqual(client.get("/api/search", params={"q": "x"}).status_code, 200)
        # fsub set + no initData -> 403 with the join list
        owner_mod.set_fsub(self.db, [{"chat_id": -1001, "title": "Ch1",
                                      "username": "ch1", "invite_link": ""}])
        r = client.get("/api/search", params={"q": "x"})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.json()["fsub_required"])
        self.assertEqual(r.json()["channels"][0]["chat_id"], -1001)

    # -- config roles ---------------------------------------------------------------
    def test_config_roles(self):
        cfg = Config()
        cfg.admin_ids = (6500072213,)
        cfg.sudo_ids = (111,)
        self.assertTrue(cfg.is_owner(6500072213))
        self.assertFalse(cfg.is_owner(111))
        self.assertTrue(cfg.is_sudo(111))
        self.assertTrue(cfg.is_privileged(6500072213))
        self.assertTrue(cfg.is_privileged(111))
        self.assertFalse(cfg.is_privileged(999))
        self.assertTrue(cfg.is_admin(6500072213))
        self.assertFalse(cfg.is_admin(111))


if __name__ == "__main__":
    unittest.main()
