"""Loading sticker for slow searches (2026-10-29).

When a search takes longer than LOADING_STICKER_DELAY_S, the bot sends an
animated loading sticker so the user knows it is working instead of
spamming. The sticker message is deleted when the result arrives.
"""
import asyncio
from types import SimpleNamespace

from app.bot import (
    LOADING_STICKER_DELAY_S,
    LOADING_STICKER_FILE_IDS,
    send_loading_sticker_later,
    settle_loading_sticker,
)


class FakeClient:
    def __init__(self, fail_sticker=False):
        self.sent = []        # (chat_id, file_id)
        self.actions = []     # (chat_id, action)
        self.fail_sticker = fail_sticker

    async def send_sticker(self, chat_id, file_id):
        if self.fail_sticker:
            raise RuntimeError("sticker boom")
        self.sent.append((chat_id, file_id))
        return SimpleNamespace(id=777)

    async def send_chat_action(self, chat_id, action):
        self.actions.append((chat_id, action))


def test_no_sticker_when_search_is_fast():
    async def go():
        c = FakeClient()
        t = asyncio.create_task(
            send_loading_sticker_later(c, 1, ["fid"], delay=60))
        await asyncio.sleep(0.05)
        assert await settle_loading_sticker(t) is None
        assert c.sent == []
    asyncio.run(go())


def test_sticker_sent_when_search_is_slow():
    async def go():
        c = FakeClient()
        t = asyncio.create_task(
            send_loading_sticker_later(c, 42, ["fid"], delay=0.05))
        await asyncio.sleep(0.3)
        assert await settle_loading_sticker(t) == 777
        assert c.sent == [(42, "fid")]
    asyncio.run(go())


def test_sticker_failure_never_raises():
    async def go():
        c = FakeClient(fail_sticker=True)
        assert await send_loading_sticker_later(c, 1, ["fid"],
                                                delay=0.01) is None
        assert c.sent == []
    asyncio.run(go())


def test_empty_file_ids_sends_nothing():
    async def go():
        c = FakeClient()
        assert await send_loading_sticker_later(c, 1, [], delay=0.01) is None
        assert c.sent == []
    asyncio.run(go())


def test_sticker_ids_are_configured():
    # Saurav's pick: loading-style stickers from the Mixpackk pack.
    assert len(LOADING_STICKER_FILE_IDS) >= 1
    assert all(isinstance(f, str) and f for f in LOADING_STICKER_FILE_IDS)
    assert LOADING_STICKER_DELAY_S > 0


def test_do_search_wires_loading_indicator():
    # _do_search must acknowledge instantly (typing) and arm the sticker.
    import pathlib
    src = pathlib.Path(__file__).parent.parent.joinpath(
        "app", "bot.py").read_text()
    body = src.split("async def _do_search")[1].split("async def ")[0]
    assert "send_chat_action" in body
    assert "send_loading_sticker_later" in body
    assert "settle_loading_sticker" in body
    assert "delete_messages" in body
