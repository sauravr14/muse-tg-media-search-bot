"""Live /indexstatus panel: auto-refreshing scan progress.

Mirrors the copy bot's /status panel: /indexstatus posts one message that
re-edits itself every few seconds with a progress bar, processed/remaining
counts, speed and ETA while a scan is running, plus per-source state.
"""
from __future__ import annotations

import asyncio
import logging
import time

from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .dbthread import run_db

log = logging.getLogger("mediabot.panel")

PANEL_TTL = 15 * 60  # stop auto-refresh 15 min after the last tap
PANEL_INTERVAL = 10


def _bar(pct: float, width: int = 12) -> str:
    filled = max(0, min(width, int(round(pct / 100 * width))))
    return "█" * filled + "░" * (width - filled)


def _dur(secs: float) -> str:
    s = int(max(0, secs))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _num(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "—"


class IndexPanel:
    def __init__(self, client, db, indexer):
        self.client = client
        self.db = db
        self.indexer = indexer
        self.chat_id = None
        self.msg_id = None
        self.task = None
        self.last_tap = 0.0
        # adopt a panel that survived a restart (ids persisted in kv)
        try:
            c = db.get_kv("panel_chat")
            m = db.get_kv("panel_msg")
            if c and m:
                self.chat_id, self.msg_id = int(c), int(m)
        except Exception:
            pass

    # -- rendering ------------------------------------------------------
    async def _text(self) -> str:
        cur = self.indexer.current_scan
        if cur:
            total = cur.get("total") or 0
            done = cur.get("done") or 0
            pct = (done / total * 100) if total else 0.0
            pct = max(0.0, min(100.0, pct))
            elapsed = time.time() - cur.get("started_at", time.time())
            speed = (done / elapsed * 60) if elapsed > 1 else 0.0
            eta = ((total - done) / (speed / 60)) if speed > 0 else 0.0
            lines = [
                "🟢 <b>Index status</b> — scanning",
                f"<code>{_bar(pct)}</code> {pct:.1f}%",
                f"📡 <b>{cur.get('title', '')}</b>",
                f"Messages: <code>{_num(done)}</code> / <code>{_num(total)}</code> "
                f"(remaining <code>{_num(total - done)}</code>)",
                f"New files indexed: <b>{_num(cur.get('new', 0))}</b>",
                f"Speed: {speed:.0f}/min · ETA: {_dur(eta)} · "
                f"elapsed: {_dur(elapsed)}",
                "",
            ]
        else:
            lines = ["⚪ <b>Index status</b> — idle", ""]
        lines.append("<b>Sources</b>")
        for s in await run_db(self.db.list_sources):
            st = s["status"] or "idle"
            icon = {"scanning": "🟢", "error": "🔴"}.get(st, "⚪")
            if not s["enabled"]:
                icon = "⏸"
                st = "paused"
            err = f" — {s['error'][:60]}" if st == "error" and s["error"] else ""
            lines.append(
                f"{icon} {s['title'] or s['chat_id']} "
                f"<code>{_num(s['last_msg_id'])}</code> · {st}{err}"
            )
        lines.append("")
        lines.append(f"<i>Updated {time.strftime('%H:%M:%S')} UTC</i>")
        return "\n".join(lines)

    def _markup(self):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data="ip:refresh"),
            InlineKeyboardButton("❌ Close", callback_data="ip:close"),
        ]])

    # -- lifecycle ------------------------------------------------------
    async def show(self, message):
        # a new /indexstatus deletes the old panel
        if self.chat_id and self.msg_id:
            try:
                await self.client.delete_messages(self.chat_id, self.msg_id)
            except Exception:
                pass
        sent = await message.reply_text(
            await self._text(), parse_mode=ParseMode.HTML, reply_markup=self._markup())
        self.chat_id, self.msg_id = sent.chat.id, sent.id
        await run_db(self.db.set_kv, "panel_chat", str(self.chat_id))
        await run_db(self.db.set_kv, "panel_msg", str(self.msg_id))
        self.last_tap = time.time()
        self._restart_loop()

    def _restart_loop(self):
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        try:
            while time.time() - self.last_tap < PANEL_TTL:
                await asyncio.sleep(PANEL_INTERVAL)
                if not self.chat_id or not self.msg_id:
                    return
                try:
                    await self.client.edit_message_text(
                        self.chat_id, self.msg_id, await self._text(),
                        parse_mode=ParseMode.HTML,
                        reply_markup=self._markup())
                except Exception as e:
                    name = type(e).__name__
                    if name == "MessageNotModified":
                        continue
                    log.warning("index panel edit failed: %r", e)
                    return  # message deleted/gone -> stop
        except asyncio.CancelledError:
            pass

    async def on_button(self, query):
        msg = query.message
        if msg:
            self.chat_id, self.msg_id = msg.chat.id, msg.id
            await run_db(self.db.set_kv, "panel_chat", str(self.chat_id))
            await run_db(self.db.set_kv, "panel_msg", str(self.msg_id))
        self.last_tap = time.time()
        data = query.data or ""
        if data == "ip:close":
            if self.task and not self.task.done():
                self.task.cancel()
            try:
                await query.message.delete()
            except Exception:
                pass
            self.chat_id = self.msg_id = None
            await run_db(self.db.set_kv, "panel_chat", "")
            await run_db(self.db.set_kv, "panel_msg", "")
            try:
                await query.answer()
            except Exception:
                pass
            return
        # ip:refresh -> restart loop (resets TTL) and repaint now
        self._restart_loop()
        try:
            await self.client.edit_message_text(
                self.chat_id, self.msg_id, await self._text(),
                parse_mode=ParseMode.HTML, reply_markup=self._markup())
        except Exception as e:
            if type(e).__name__ != "MessageNotModified":
                log.warning("index panel button edit failed: %r", e)
        try:
            await query.answer()
        except Exception:
            pass
