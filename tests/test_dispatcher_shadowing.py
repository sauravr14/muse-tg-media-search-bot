"""Regression tripwire for the 2026-10-28 reply outage.

Pyrogram's dispatcher stops at the FIRST matching handler in a group
(it breaks after one match). The owner-features deploy added a
catch-all tracking handler registered first in group 0, so it shadowed
/start, /id, /search, /stats and every other private handler: messages
were tracked but no reply ever ran, for admin and non-admin alike,
while Mini App API (which bypasses the dispatcher) kept working.

Guard: the tracking handler and the pending-prompt handler must raise
ContinuePropagation (not return normally) so the dispatcher moves on
to the real handler for the message. The nested handlers are wrapped
by Pyrogram's sync layer, so this test guards the exact lines in
bot.py rather than invoking the wrapped callbacks.
"""
import re
import unittest
from pathlib import Path

BOT_PY = Path(__file__).resolve().parent.parent / "app" / "bot.py"


def _fn_body(src: str, name: str) -> str:
    # Body from "async def <name>" up to the next handler decorator.
    m = re.search(rf"async def {name}\b(.*?)(?=\n        @c\.|\n    def |\Z)",
                  src, re.S)
    assert m, f"{name} not found in bot.py"
    return m.group(1)


class ShadowingTest(unittest.TestCase):
    def test_track_handlers_continue(self):
        src = BOT_PY.read_text()
        for name in ("track_private", "track_group"):
            body = _fn_body(src, name)
            self.assertIn("raise ContinuePropagation", body,
                          f"{name} must raise ContinuePropagation, "
                          "or it shadows every later private handler")

    def test_pending_owner_continues_when_not_handling(self):
        src = BOT_PY.read_text()
        body = _fn_body(src, "pending_owner")
        # Both early exits (no pending prompt, and commands bypassing a
        # prompt) must continue, not return normally and shadow the
        # real command/search handler.
        self.assertGreaterEqual(body.count("raise ContinuePropagation"), 2)
        # Handling a real pending prompt must still stop propagation.
        self.assertIn("raise StopPropagation", body)
