"""Telegram bot: search, browse, deep links, file delivery, admin commands.

Runs on the bot token (PyroFork). Plain text in private chat = search.
File delivery copies the source channel message into the user's chat —
the bot must be admin in every source channel.
"""
from __future__ import annotations

import asyncio
import base64
import html
import logging
import re
import time

from pyrogram import Client, ContinuePropagation, StopPropagation, filters
from pyrogram.enums import ParseMode
import aiohttp

from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message, WebAppInfo)

from . import owner as owner_mod
from .config import Config
from .db import DB, RES_ORDER
from .dbthread import run_db, run_db_fast
from .indexer import Indexer

log = logging.getLogger("mediabot.bot")
PAGE = 6
LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(c/)?([A-Za-z0-9_]+|\d+)/(\d+)/?$"
)


def _parse_post_link(text: str):
    """t.me/<channel>/<id> or t.me/c/<digits>/<id> -> (chat_id, msg_id)."""
    m = LINK_RE.search((text or "").strip())
    if not m:
        return None
    is_private, name, msg_id = m.group(1), m.group(2), int(m.group(3))
    if is_private:
        return -int(f"100{name}"), msg_id
    return f"@{name}", msg_id


async def _is_forwarded(_, __, m) -> bool:
    # filters.forwarded only checks forward_date, which can be None/0 even
    # when the message IS forwarded (seen in the wild on Pyrogram builds).
    # Check every origin attribute so forwarded channel posts are never
    # silently missed by the index flow.
    return bool(
        getattr(m, "forward_date", None)
        or getattr(m, "forward_from_chat", None)
        or getattr(m, "forward_from", None)
        or getattr(m, "forward_sender_name", None)
    )


is_forwarded = filters.create(_is_forwarded)


def _fmt_size(n: int) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def _title_line(t) -> str:
    year = f" ({t['year']})" if t["year"] else ""
    kind = "🎬" if t["kind"] == "movie" else "📺"
    return f"{kind} <b>{html.escape(t['title'])}</b>{year}"


def _search_start_param(q: str) -> str:
    """Encode a search query into a Mini App start_param.

    Telegram start_params allow only [A-Za-z0-9_-] and at most 64 chars,
    so the query is base64url-encoded (padding stripped) with a 'q_'
    prefix; the Mini App decodes it and runs the search immediately, so
    the button lands the user on results, not on a blank search box.
    Returns '' when the encoded form would not fit — the button then
    opens the app without a prefilled query instead of a broken param.
    """
    enc = base64.urlsafe_b64encode(q.strip().encode("utf-8")).decode().rstrip("=")
    param = f"q_{enc}"
    return param if len(param) <= 64 else ""


class MediaBot:
    def __init__(self, cfg: Config, db: DB, indexer: Indexer):
        self.cfg = cfg
        self.db = db
        self.indexer = indexer
        # workers=32 (Pyrogram default is 6): with only 6 dispatcher
        # workers, a few slow handlers (live channel ingest, a large file
        # delivery) starved user commands entirely in the 2026-10-28
        # outage; 16 helped, but the 3-4 minute reply delays the same
        # day showed bursts still queue. More workers give user commands
        # headroom while background work runs; the real fix is still the
        # fire-and-forget handlers + fast commits + the reserved reply
        # DB executor, this is just margin.
        self.client = Client("mediabot", api_id=cfg.api_id, api_hash=cfg.api_hash,
                             bot_token=cfg.bot_token, in_memory=True,
                             workers=32)
        self.username = ""
        self.panel = None  # IndexPanel, attached in main.py once indexer exists
        self._spam: dict[int, float] = {}
        self._log_q = None
        self._log_ch = 0
        self._log_ch_at = -999.0
        self._stats_cache = (0.0, None)
        self._me_id = 0
        # owner-only pending prompts: user_id -> {"kind": ..., "ts": ...}
        # kinds: start_photo, fsub_add, log_set, broadcast (draft kept in
        # _bc_draft). Expires after 10 min; /cancel clears it.
        self._pending: dict[int, dict] = {}
        self._bc_draft: dict[int, dict] = {}
        self._register()

    # -- search ---------------------------------------------------------------
    def search_titles(self, q: str, kind: str = "", limit: int = 12, offset: int = 0):
        q = q.strip()
        if not q:
            return []
        # quote for FTS5; prefix match each term
        terms = [t for t in q.replace('"', "").split() if t]
        if not terms:
            return []
        match = " ".join(f'"{t}"*' for t in terms)
        sql = ("SELECT t.* FROM titles_fts f JOIN titles t ON t.id=f.rowid "
               "WHERE titles_fts MATCH ?")
        params: list = [match]
        if kind in ("movie", "series"):
            sql += " AND t.kind=?"
            params.append(kind)
        sql += " ORDER BY t.popularity DESC NULLS LAST, t.last_file_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        try:
            return self.db.query(sql, tuple(params))
        except Exception as e:
            log.warning("fts failed for %r: %r", q, e)
            like = f"%{'%'.join(terms)}%"
            return self.db.query(
                "SELECT * FROM titles WHERE norm_title LIKE ? "
                + ("AND kind=? " if kind in ("movie", "series") else "")
                + "ORDER BY last_file_at DESC LIMIT ? OFFSET ?",
                tuple([like.lower()] + ([kind] if kind in ("movie", "series") else []) + [limit, offset]),
            )

    def _versions(self, title_id: int, episode_id: int | None = None):
        if episode_id:
            return self.db.query(
                "SELECT * FROM files WHERE episode_id=? AND status='ok' "
                "ORDER BY " + RES_ORDER + " DESC", (episode_id,))
        return self.db.query(
            "SELECT * FROM files WHERE title_id=? AND episode_id IS NULL "
            "AND season_id IS NULL AND status='ok' "
            "ORDER BY " + RES_ORDER + " DESC", (title_id,))

    def _kb_versions(self, files, prefix="getf"):
        rows = []
        for f in files:
            label = f"{f['resolution'] or '?'} · {_fmt_size(f['size'])}"
            rows.append([InlineKeyboardButton(f"📥 {label}",
                                              callback_data=f"{prefix}:{f['id']}")])
        return InlineKeyboardMarkup(rows) if rows else None

    # -- mini app ---------------------------------------------------------------
    def _app_url(self, start_param: str = "") -> str:
        base = self.cfg.mini_app_url
        return base + (f"?startapp={start_param}" if start_param else "")

    def _app_button(self, text: str, start_param: str = "") -> InlineKeyboardButton:
        """Real Mini App button (https) or plain link fallback (http)."""
        if self.cfg.mini_app_url.startswith("https://"):
            return InlineKeyboardButton(text, web_app=WebAppInfo(url=self._app_url(start_param)))
        # http fallback: plain link, deep link via hash for browsers
        url = self.cfg.mini_app_url
        if start_param.startswith("title_"):
            url += f"#/title/{start_param[6:]}"
        elif start_param.startswith("q_"):
            url += f"#/search/{start_param[2:]}"
        return InlineKeyboardButton(text, url=url)

    async def _set_menu_button(self) -> None:
        """Puts the Mini App behind the bot's menu button (needs https URL)."""
        if not self.cfg.mini_app_url.startswith("https://"):
            log.warning("MINI_APP_URL is not https — menu button skipped "
                        "(Telegram requires https for Mini Apps)")
            return
        api = f"https://api.telegram.org/bot{self.cfg.bot_token}/setChatMenuButton"
        payload = {"menu_button": {"type": "web_app", "text": "Media Library",
                                   "web_app": {"url": self.cfg.mini_app_url}}}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(api, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    log.info("setChatMenuButton -> %s", (await r.text())[:120])
        except Exception as e:
            log.warning("setChatMenuButton failed: %r", e)

    # -- owner features -----------------------------------------------------------
    def _is_owner(self, user_id) -> bool:
        return self.cfg.is_owner(user_id)

    def _is_privileged(self, user_id) -> bool:
        return self.cfg.is_privileged(user_id)

    async def _self_id(self) -> int:
        if not self._me_id:
            try:
                me = await self.client.get_me()
                self._me_id = me.id or 0
            except Exception:
                pass
        return self._me_id

    def _pending_get(self, user_id: int):
        st = self._pending.get(user_id)
        if not st or time.time() - st.get("ts", 0) > 600:
            self._pending.pop(user_id, None)
            return None
        return st

    def _pending_set(self, user_id: int, kind: str) -> None:
        self._pending[user_id] = {"kind": kind, "ts": time.time()}

    def _pending_clear(self, user_id: int) -> None:
        self._pending.pop(user_id, None)
        self._bc_draft.pop(user_id, None)

    @staticmethod
    def _chat_type_name(chat) -> str:
        t = getattr(chat, "type", "") or ""
        if hasattr(t, "value"):
            t = t.value
        return str(t).lower().split(".")[-1]

    async def _track_private(self, m: Message) -> None:
        u = m.from_user
        if not u or getattr(u, "is_bot", False):
            return
        await self._log_user_message(m)
        try:
            await run_db(self.db.upsert_user, u.id, u.username or "",
                         u.first_name or "", u.last_name or "",
                         m.text or m.caption or "")
        except Exception as e:
            log.warning("user upsert failed: %r", e)

    async def _log_channel(self) -> int:
        now = time.monotonic()
        if now - self._log_ch_at > 15:
            self._log_ch = await run_db_fast(owner_mod.resolve_log_channel,
                                             self.db, self.cfg.log_channel_id)
            self._log_ch_at = now
        return self._log_ch

    async def _log_user_message(self, m: Message) -> None:
        u = m.from_user
        if not u or not owner_mod.should_log_user(
                u.id, self._is_privileged(u.id)):
            return
        try:
            channel = await self._log_channel()
            if not channel:
                return
            text = m.text or m.caption or "[non-text message]"
            msg = owner_mod.format_log_message(
                time.time(), u.id, u.username or "",
                f"{u.first_name or ''} {u.last_name or ''}".strip(), text)
            if self._log_q is None:
                self._log_q = asyncio.Queue(maxsize=500)
                asyncio.create_task(self._log_sender())
            try:
                self._log_q.put_nowait((channel, msg))
            except asyncio.QueueFull:
                pass  # logs are best-effort; never slow replies
        except Exception as e:
            log.warning("live log failed: %r", e)

    async def _log_sender(self) -> None:
        """One sender for all live-log lines: ordered, and a Telegram
        flood-wait on the log channel only delays logs, never replies."""
        while True:
            channel, msg = await self._log_q.get()
            try:
                await self.client.send_message(channel, msg,
                                               parse_mode=ParseMode.HTML)
            except Exception as e:
                log.warning("live log failed: %r", e)
            await asyncio.sleep(0.4)

    async def _unjoined(self, user_id: int) -> list:
        entries = await run_db_fast(owner_mod.fsub_list, self.db)
        return await owner_mod.unjoined_channels(
            self.client, user_id, entries, self._is_privileged(user_id))

    async def _send_fsub_prompt(self, chat_id: int, unjoined: list) -> None:
        await self.client.send_message(
            chat_id,
            "🔒 <b>Join these channels first</b> to use the bot and the "
            "Media Library app, then tap “I've joined” below.",
            parse_mode=ParseMode.HTML,
            reply_markup=owner_mod.fsub_join_keyboard(unjoined))

    async def unjoined_fsub(self, user_id: int) -> list:
        """Used by the Mini App API (api.py)."""
        return await self._unjoined(user_id)

    def _start_kb(self, is_owner: bool) -> InlineKeyboardMarkup:
        return owner_mod.build_start_keyboard(self._app_button, is_owner)

    async def _send_start(self, chat_id: int, user_id: int) -> None:
        t0 = time.monotonic()
        unjoined = await self._unjoined(user_id)
        if unjoined:
            await self._send_fsub_prompt(chat_id, unjoined)
            return
        text, photo = await asyncio.gather(
            run_db_fast(owner_mod.get_start_text, self.db),
            run_db_fast(owner_mod.get_start_photo, self.db))
        kb = self._start_kb(self._is_owner(user_id))
        if photo:
            try:
                await self.client.send_photo(chat_id, photo,
                                             caption=text[:1024],
                                             parse_mode=ParseMode.HTML,
                                             reply_markup=kb)
                return
            except Exception as e:
                log.warning("start photo failed, falling back to text: %r", e)
        await self.client.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                       reply_markup=kb)
        dt = time.monotonic() - t0
        if dt > 10:
            log.warning("slow /start reply: %.1fs for user %s", dt, user_id)

    # -- handlers ---------------------------------------------------------------
    def _register(self):
        c = self.client
        admin_ids = list(self.cfg.admin_ids)
        priv_ids = list(self.cfg.admin_ids) + list(self.cfg.sudo_ids)

        # Tracking runs first and never stops other handlers: every private
        # message records the user (+ live log), every group message records
        # the chat. Bot's own messages are never recorded.
        @c.on_message(filters.private)
        async def track_private(_, m: Message):
            # Fire-and-forget, never awaited: awaiting a DB write here
            # would also delay replies while a slow commit held the
            # SQLite write lock. Tracking is best-effort: it completes
            # when the lock frees, and a lost update on restart costs
            # one row, not a reply.
            # CRITICAL (2026-10-28 outage): Pyrogram stops at the FIRST
            # matching handler in a group (it breaks after one match).
            # This handler matches EVERY private message and is registered
            # first, so without ContinuePropagation it shadowed /start,
            # /id, /search, /stats and every other private handler —
            # messages were tracked (users table grew) but no reply ever
            # ran, for admin and non-admin alike, while Mini App API
            # (which bypasses the dispatcher) kept working. Raising
            # ContinuePropagation lets the dispatcher move on to the
            # real handler for this message.
            asyncio.create_task(self._track_private(m))
            raise ContinuePropagation

        @c.on_message(filters.group)
        async def track_group(_, m: Message):
            # Same fire-and-forget reason as track_private above.
            async def _track_group():
                try:
                    await run_db(self.db.upsert_chat, m.chat.id,
                                 self._chat_type_name(m.chat),
                                 m.chat.title or "", m.chat.username or "")
                except Exception as e:
                    log.warning("chat upsert failed: %r", e)
            asyncio.create_task(_track_group())
            # Same first-match-break reason as track_private above.
            raise ContinuePropagation

        @c.on_chat_member_updated()
        async def track_member(_, u):
            try:
                new = u.new_chat_member
                if not new or not new.user:
                    return
                me_id = await self._self_id()
                if not me_id or new.user.id != me_id:
                    return
                status = getattr(new, "status", "")
                if hasattr(status, "value"):
                    status = status.value
                status = str(status).lower().split(".")[-1]
                if status in ("member", "administrator", "creator", "restricted"):
                    await run_db(self.db.upsert_chat, u.chat.id,
                                 self._chat_type_name(u.chat),
                                 u.chat.title or "", u.chat.username or "")
                elif status in ("left", "kicked"):
                    await run_db(self.db.execute,
                                 "DELETE FROM chats WHERE chat_id=?",
                                 (u.chat.id,))
            except Exception as e:
                log.warning("member track failed: %r", e)

        # Owner-only pending prompts (settings flows). Consumes the message
        # so it never also triggers a search or an index flow; /cancel or a
        # command bypasses it.
        @c.on_message(filters.private & filters.user(admin_ids)
                       & (filters.text | filters.photo))
        async def pending_owner(_, m: Message):
            uid = m.from_user.id if m.from_user else 0
            st = self._pending_get(uid)
            if not st:
                # No pending prompt: this handler must NOT shadow the
                # real command/search handler (same first-match-break
                # trap as track_private — it matches every admin private
                # text/photo message). Let the dispatcher continue.
                raise ContinuePropagation
            if (m.text or "").startswith("/"):
                # Commands bypass a pending prompt (e.g. /cancel, /start)
                # — again, continue instead of shadowing the command.
                raise ContinuePropagation
            try:
                await self._handle_pending(m, st["kind"])
            except Exception as e:
                log.warning("pending prompt failed: %r", e)
            raise StopPropagation

        @c.on_message(filters.private & filters.command("start"))
        async def start(_, m: Message):
            arg = (m.text or "").split(maxsplit=1)
            if len(arg) > 1 and arg[1].startswith("file_"):
                await self._deliver_file(m.chat.id, arg[1][5:], m.from_user.id)
                return
            await self._send_start(m.chat.id, m.from_user.id)

        @c.on_message(filters.private & filters.command("id"))
        async def whoami(_, m: Message):
            await m.reply_text(f"Your Telegram user id: <code>{m.from_user.id}</code>",
                               parse_mode=ParseMode.HTML)

        @c.on_message(filters.private & filters.command("help"))
        async def help_(_, m: Message):
            await m.reply_text(
                "🔎 <b>Search</b> — just type a movie/series name.\n"
                "/search <name> — search the catalog\n"
                "/recent — recently added\n"
                "/movies · /series — browse\n\n"
                "Search replies with <b>one message and one button</b> — tap it "
                "and the app opens straight on your results, with posters, all "
                "qualities (480p/720p/1080p) grouped under one title, and "
                "Get / Play. Files you get self-destruct in 10 minutes, "
                "so forward them to your saved messages.\n\n"
                "Admin: /addchannel · /rmchannel · /channels · /index · "
                "/reparse · /setskip · /stats · /settings · /users · /broadcast",
                parse_mode=ParseMode.HTML)

        @c.on_message(filters.private & filters.command("search"))
        async def search_cmd(_, m: Message):
            q = m.text.partition(" ")[2].strip()
            if not q:
                await m.reply_text("Usage: /search <movie or series name>")
                return
            await self._do_search(m.chat.id, q, m.from_user.id)

        @c.on_message(filters.private & filters.command("recent"))
        async def recent(_, m: Message):
            unjoined = await self._unjoined(m.from_user.id)
            if unjoined:
                await self._send_fsub_prompt(m.chat.id, unjoined)
                return
            rows = await run_db_fast(
                self.db.query,
                "SELECT * FROM titles ORDER BY (poster != '') DESC, "
                "last_file_at DESC LIMIT 8")
            if not rows:
                await m.reply_text("Nothing indexed yet.")
                return
            # One message, like search — the old version sent a card per
            # title (9 messages) and flooded the chat the same way.
            lines = ["<b>🆕 Recently added</b>"]
            lines += [_title_line(t) for t in rows]
            lines.append("\nOpen the app to browse them with posters.")
            kb = InlineKeyboardMarkup([[self._app_button("🌐 Open Media Library")]])
            await m.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                               reply_markup=kb)

        @c.on_message(filters.private & filters.command(["movies", "series"]))
        async def browse(_, m: Message):
            unjoined = await self._unjoined(m.from_user.id)
            if unjoined:
                await self._send_fsub_prompt(m.chat.id, unjoined)
                return
            kind = "movie" if m.command[0] == "movies" else "series"
            rows = await run_db_fast(
                self.db.query,
                "SELECT * FROM titles WHERE kind=? ORDER BY title LIMIT ?",
                (kind, PAGE))
            if not rows:
                await m.reply_text("Nothing here yet.")
                return
            lines = [f"<b>{'🎬 Movies' if kind=='movie' else '📺 Series'}</b>"]
            for t in rows:
                year = f" ({t['year']})" if t["year"] else ""
                lines.append(f"• {html.escape(t['title'])}{year}")
            await m.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

        # ---- forward a channel post (or send a t.me post link) to index it ----
        # NOTE: uses the robust is_forwarded check (all origin attributes),
        # not filters.forwarded — forward_date alone can be None/0 on some
        # Pyrogram builds and the forward would be silently ignored.
        @c.on_message(filters.private & is_forwarded & filters.user(priv_ids))
        async def forward_index(_, m: Message):
            fwd = m.forward_from_chat
            fwd_id = m.forward_from_message_id
            if fwd and fwd_id:
                await self._prompt_index(m.chat.id, fwd.id, fwd_id,
                                         fwd.title or str(fwd.id))
            else:
                await m.reply_text(
                    "I couldn't read that forward (the channel may restrict forwarding). "
                    "Send a post link instead: <code>https://t.me/channelname/123</code>",
                    parse_mode=ParseMode.HTML)

        @c.on_message(filters.private & filters.text & filters.user(priv_ids)
                       & filters.regex(LINK_RE))
        async def link_index(_, m: Message):
            parsed = _parse_post_link(m.text)
            if not parsed:
                return
            chat_id, msg_id = parsed
            try:
                chat = await self.client.get_chat(chat_id)
                title = chat.title or str(chat_id)
            except Exception:
                title = str(chat_id)
            await self._prompt_index(m.chat.id, chat_id, msg_id, title)

        @c.on_message(filters.private & filters.text & ~filters.regex(r"^/"))
        async def plain_search(_, m: Message):
            await self._do_search(m.chat.id, m.text.strip(), m.from_user.id)

        @c.on_callback_query()
        async def callbacks(_, q: CallbackQuery):
            data = q.data or ""
            try:
                if data.startswith("getf:"):
                    await q.answer("Sending…")
                    await self._deliver_file(q.message.chat.id, data[5:], q.from_user.id)
                elif data.startswith("seasons:"):
                    await self._show_seasons(q, int(data[8:]))
                elif data.startswith("season:"):
                    await self._show_episodes(q, int(data[7:]))
                elif data.startswith("ep:"):
                    await self._show_episode(q, int(data[3:]))
                elif data.startswith("idx_yes:"):
                    _, cid, top = data.split(":")
                    await self._index_yes(q, int(cid), int(top))
                elif data.startswith("ip:"):
                    if self.panel:
                        await self.panel.on_button(q)
                    else:
                        await q.answer("Panel not ready yet.")
                elif data == "idx_no":
                    await q.answer("Cancelled")
                    try:
                        await q.message.edit_text("Cancelled.")
                    except Exception:
                        pass
                elif data in ("help", "about"):
                    await self._help_about_callback(q, data)
                elif data == "back_start":
                    await q.answer()
                    try:
                        await q.message.delete()
                    except Exception:
                        pass
                    await self._send_start(q.message.chat.id, q.from_user.id)
                elif data == "fsub_check":
                    owner_mod.fsub_forget(q.from_user.id)
                    unjoined = await self._unjoined(q.from_user.id)
                    if unjoined:
                        await q.answer("You haven't joined all channels yet.",
                                       show_alert=True)
                    else:
                        await q.answer("Welcome back!")
                        try:
                            await q.message.delete()
                        except Exception:
                            pass
                        await self._send_start(q.message.chat.id, q.from_user.id)
                elif data.startswith("settings:") or data.startswith("fsub:") \
                        or data.startswith("log:") or data.startswith("users:") \
                        or data.startswith("bc:"):
                    if not self._is_owner(q.from_user.id):
                        await q.answer("Owner only.", show_alert=True)
                    else:
                        await self._owner_callback(q, data)
                else:
                    await q.answer()
            except Exception as e:
                log.warning("callback failed: %r", e)
                try:
                    await q.answer("Something went wrong.", show_alert=True)
                except Exception:
                    pass

        # ---- admin ----        # ---- admin ----
        self._register_admin(c)
        # live indexing of new channel posts — fire-and-forget, never
        # awaited in the handler. Awaited live ingest does DB writes
        # (note_seen + ingest), and while a slow commit held the write
        # lock it occupied a dispatcher worker task; with posts arriving
        # from many channels, all workers starved and no user command
        # got a reply at all (2026-10-28 outage, while Mini App reads
        # kept working). on_channel_post already catches and logs its
        # own failures, so a background task is safe here.
        @c.on_message(filters.channel)
        async def live(_, m: Message):
            asyncio.create_task(self.indexer.on_channel_post(_, m))

    def _register_admin(self, c):
        admin_ids = list(self.cfg.admin_ids)
        priv_ids = list(self.cfg.admin_ids) + list(self.cfg.sudo_ids)

        @c.on_message(filters.private & filters.command("addchannel") & filters.user(priv_ids))
        async def addchannel(_, m: Message):
            arg = m.text.partition(" ")[2].strip()
            if not arg:
                await m.reply_text("Usage: /addchannel <channel_id or @username>")
                return
            # private channels have no username — accept the -100... id
            target: int | str = int(arg) if arg.lstrip("-").isdigit() else arg
            try:
                chat = await self.client.get_chat(target)
            except Exception as e:
                await m.reply_text(f"Can't access that chat: {e}")
                return
            await run_db(self.db.upsert_source, chat.id, chat.title or "",
                         chat.username or "")
            await m.reply_text(f"✅ Added <b>{html.escape(chat.title or arg)}</b> "
                               f"(<code>{chat.id}</code>).\n\n"
                               f"To start indexing, <b>forward any message</b> from that "
                               f"channel here (or send a <code>t.me</code> post link) "
                               f"and tap ✅ Yes.",
                               parse_mode=ParseMode.HTML)

        @c.on_message(filters.private & filters.command("rmchannel") & filters.user(admin_ids))
        async def rmchannel(_, m: Message):
            arg = m.text.partition(" ")[2].strip()
            if not arg:
                await m.reply_text("Usage: /rmchannel <channel_id>")
                return
            try:
                cid = int(arg)
            except ValueError:
                await m.reply_text("Pass the numeric channel id.")
                return
            await run_db(self.db.remove_source, cid)
            await m.reply_text(f"Removed <code>{cid}</code>.", parse_mode=ParseMode.HTML)

        @c.on_message(filters.private & filters.command("channels") & filters.user(priv_ids))
        async def channels(_, m: Message):
            rows = await run_db(self.db.list_sources)
            if not rows:
                await m.reply_text("No sources configured.")
                return
            lines = ["<b>📡 Sources</b>"]
            for s in rows:
                n = (await run_db(
                    self.db.query_one,
                    "SELECT COUNT(*) c FROM files WHERE tg_chat_id=?",
                    (s["chat_id"],)))["c"]
                icon = "🟢" if s["enabled"] else "⚪"
                lines.append(f"{icon} {html.escape(s['title'] or str(s['chat_id']))} "
                             f"— {n} files — {s['status']}")
            await m.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

        @c.on_message(filters.private & filters.command("index") & filters.user(priv_ids))
        async def index(_, m: Message):
            await m.reply_text("🔄 Full scan started in the background — "
                               "I'll report here when it's done.")
            asyncio.create_task(self._index_all_report(m.chat.id))

        @c.on_message(filters.private & filters.command("indexstatus") & filters.user(priv_ids))
        async def indexstatus(_, m: Message):
            if self.panel:
                await self.panel.show(m)
            else:
                await m.reply_text("Panel not ready yet — try again in a few seconds.")

        @c.on_message(filters.private & filters.command("stopindex") & filters.user(priv_ids))
        async def stopindex(_, m: Message):
            """Stop the running scan (/stopindex [channel_id]); the scan
            finishes its current batch, keeps its checkpoint, and stays
            idle until you start it again."""
            args = m.text.split()[1:]
            target = None
            if args:
                try:
                    target = int(args[0])
                except ValueError:
                    await m.reply_text("Usage: /stopindex [channel_id]")
                    return
            else:
                cur = self.indexer.current_scan
                target = cur["chat_id"] if cur else None
            if target is None:
                await m.reply_text("No scan is running right now.")
                return
            self.indexer.request_stop(target)
            src = await run_db(self.db.get_source, target) or {}
            name = src.get("title") or str(target)
            await m.reply_text(f"🛑 Stopping the scan of <b>{html.escape(name)}</b> — "
                               f"it will halt after its current batch and keep its checkpoint.")

        @c.on_message(filters.private & filters.command("reparse") & filters.user(admin_ids))
        async def reparse(_, m: Message):
            await m.reply_text("🔄 Re-parsing all filenames and regrouping titles — "
                               "I'll report here when it's done.")
            asyncio.create_task(self._reparse_report(m.chat.id))

        @c.on_message(filters.private & filters.command("cleanup") & filters.user(admin_ids))
        async def cleanup(_, m: Message):
            await m.reply_text("🧹 Cleaning up mis-grouped files — only files whose "
                               "title changed under the current parser get moved; "
                               "posters and matches stay intact. I'll report when done.")
            asyncio.create_task(self._cleanup_report(m.chat.id))

        @c.on_message(filters.private & filters.command("setskip") & filters.user(priv_ids))
        async def setskip(_, m: Message):
            """Skip the first N messages of a channel when scanning.

            /setskip                  — show current values
            /setskip <number>         — set for all sources
            /setskip <channel_id> <number> — set for one source
            Applies to fresh scans (before last_msg_id passes the skip point).
            """
            args = m.text.split()[1:]
            if not args:
                rows = await run_db(self.db.list_sources)
                if not rows:
                    await m.reply_text("No sources configured.")
                    return
                lines = ["<b>⏭ Skip settings</b> (messages skipped at scan start)"]
                for s in rows:
                    lines.append(f"• <code>{s['chat_id']}</code> "
                                 f"{html.escape(s['title'] or '')} — "
                                 f"<b>{s['skip_first'] or 0}</b>")
                await m.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
                return
            if len(args) == 1:
                try:
                    n = int(args[0])
                except ValueError:
                    await m.reply_text("Usage: /setskip <number> "
                                       "or /setskip <channel_id> <number>")
                    return
                if n < 0:
                    await m.reply_text("Number must be 0 or more.")
                    return
                await run_db(self.db.execute,
                             "UPDATE sources SET skip_first=?", (n,))
                await m.reply_text(f"✅ Skip set to <b>{n}</b> for all sources.",
                                   parse_mode=ParseMode.HTML)
                return
            try:
                cid, n = int(args[0]), int(args[1])
            except ValueError:
                await m.reply_text("Usage: /setskip <number> "
                                   "or /setskip <channel_id> <number>")
                return
            if n < 0:
                await m.reply_text("Number must be 0 or more.")
                return
            await run_db(self.db.execute,
                         "UPDATE sources SET skip_first=? WHERE chat_id=?",
                         (n, cid))
            await m.reply_text(f"✅ Skip set to <b>{n}</b> for <code>{cid}</code>.",
                               parse_mode=ParseMode.HTML)

        @c.on_message(filters.private & filters.command("stats") & filters.user(priv_ids))
        async def stats(_, m: Message):
            ts, s = self._stats_cache
            if s is None or time.monotonic() - ts > 30:
                s = await run_db_fast(self.db.stats)
                self._stats_cache = (time.monotonic(), s)
            await m.reply_text(
                f"📊 <b>Catalog stats</b>\n"
                f"Files: <b>{s['files']}</b> · Movies: <b>{s['movies']}</b> · "
                f"Series: <b>{s['series']}</b> · Episodes: <b>{s['episodes']}</b>\n"
                f"Sources: <b>{s['sources']}</b> · Unmatched: <b>{s['unmatched']}</b>",
                parse_mode=ParseMode.HTML)

        # ---- owner-only: settings, users, broadcast, cancel ----
        @c.on_message(filters.private & filters.command("settings") & filters.user(admin_ids))
        async def settings_cmd(_, m: Message):
            await self._settings_send(m.chat.id)

        @c.on_message(filters.private & filters.command("cancel") & filters.user(admin_ids))
        async def cancel_cmd(_, m: Message):
            self._pending_clear(m.from_user.id)
            await m.reply_text("Cancelled — no setting was changed.")

        @c.on_message(filters.private & filters.command("users") & filters.user(admin_ids))
        async def users_cmd(_, m: Message):
            await self._users_send(m.chat.id, 0)

        @c.on_message(filters.private & filters.command("broadcast") & filters.user(admin_ids))
        async def broadcast_cmd(_, m: Message):
            text = m.text.partition(" ")[2].strip()
            if not text:
                self._pending_set(m.from_user.id, "broadcast")
                await m.reply_text(
                    "📣 Send the broadcast message now (text, or a photo with a "
                    "caption). I'll show a preview with Send / Cancel before "
                    "anything goes out. /cancel to abort.")
                return
            self._bc_draft[m.from_user.id] = {"text": text, "photo": None}
            await self._broadcast_preview(m.chat.id, m.from_user.id)

    # -- owner flows (settings, users, broadcast, pending prompts) --------------
    HELP_TEXT = (
        "🔎 <b>Search</b> — just type a movie/series name.\n"
        "/search &lt;name&gt; — search the catalog\n"
        "/recent — recently added\n"
        "/movies · /series — browse\n\n"
        "Search replies with <b>one message and one button</b> — tap it "
        "and the app opens straight on your results, with posters, all "
        "qualities (480p/720p/1080p) grouped under one title, and "
        "Get / Play. Files you get self-destruct in 10 minutes, "
        "so forward them to your saved messages.")
    ABOUT_TEXT = (
        "🎬 <b>Core TG Media Library</b>\n\n"
        "Find movies & series from our channels, instantly, with posters "
        "and every quality grouped under one title.\n\n"
        "Just type a movie or series name in this chat, or open the app.")

    async def _help_about_callback(self, q: CallbackQuery, which: str) -> None:
        text = self.HELP_TEXT if which == "help" else self.ABOUT_TEXT
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "⬅ Back", callback_data="back_start")]])
        try:
            await q.message.edit_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=kb)
        except Exception:
            await q.answer("Open the start card again with /start.")
            return
        await q.answer()

    async def _settings_text(self) -> str:
        photo = await run_db(owner_mod.get_start_photo, self.db)
        fsub = await run_db(owner_mod.fsub_list, self.db)
        log_ch = await run_db(owner_mod.resolve_log_channel, self.db,
                              self.cfg.log_channel_id)
        return (
            "⚙️ <b>Settings</b> (owner only)\n\n"
            f"📸 Start photo: {'set ✅' if photo else 'not set'}\n"
            f"🔒 Force subscribe: {len(fsub)} channel(s)\n"
            f"📋 Live logs: {'<code>' + str(log_ch) + '</code>' if log_ch else 'off'}\n"
            f"👥 Users recorded: <b>{await run_db(self.db.count_users)}</b>")

    def _settings_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📸 Start Photo", callback_data="settings:photo")],
            [InlineKeyboardButton("🔒 Force Subscribe", callback_data="settings:fsub")],
            [InlineKeyboardButton("📋 Live Logs", callback_data="settings:logs")],
            [InlineKeyboardButton("👥 Users", callback_data="settings:users"),
             InlineKeyboardButton("📣 Broadcast", callback_data="settings:broadcast")],
        ])

    async def _settings_send(self, chat_id: int) -> None:
        await self.client.send_message(chat_id, await self._settings_text(),
                                       parse_mode=ParseMode.HTML,
                                       reply_markup=self._settings_kb())

    async def _edit_or_send(self, q: CallbackQuery, text: str,
                            kb: InlineKeyboardMarkup) -> None:
        try:
            await q.message.edit_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=kb)
        except Exception:
            await self.client.send_message(q.message.chat.id, text,
                                           parse_mode=ParseMode.HTML,
                                           reply_markup=kb)
        await q.answer()

    async def _users_text(self, offset: int) -> tuple[str, InlineKeyboardMarkup]:
        total = await run_db(self.db.count_users)
        rows = await run_db(self.db.list_users, owner_mod.USERS_PAGE, offset)
        lines = [f"👥 <b>Users</b> — {total} total "
                 f"(showing {offset + 1}–{offset + len(rows)} of {total})"]
        if not rows:
            lines.append("\nNo users recorded yet. Users are recorded from "
                         "this deploy onward — Telegram gives no retroactive "
                         "list of past users.")
        for r in rows:
            name = html.escape(f"{r['first_name'] or ''} {r['last_name'] or ''}".strip()
                               or r["username"] or "—")
            who = f"@{r['username']}" if r["username"] else name
            first = time.strftime("%Y-%m-%d", time.localtime(r["first_seen"])) \
                if r["first_seen"] else "—"
            last = time.strftime("%Y-%m-%d", time.localtime(r["last_seen"])) \
                if r["last_seen"] else "—"
            lines.append(f"• <code>{r['user_id']}</code> {html.escape(who)} "
                         f"({first} → {last})")
        kb_rows: list = []
        nav: list = []
        if offset > 0:
            nav.append(InlineKeyboardButton("⬅ Prev",
                                            callback_data=f"users:{max(0, offset - owner_mod.USERS_PAGE)}"))
        if offset + owner_mod.USERS_PAGE < total:
            nav.append(InlineKeyboardButton("Next ➡",
                                            callback_data=f"users:{offset + owner_mod.USERS_PAGE}"))
        if nav:
            kb_rows.append(nav)
        kb_rows.append([InlineKeyboardButton("⬅ Settings", callback_data="settings:main")])
        return "\n".join(lines), InlineKeyboardMarkup(kb_rows)

    async def _users_send(self, chat_id: int, offset: int) -> None:
        text, kb = await self._users_text(offset)
        await self.client.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                       reply_markup=kb)

    async def _broadcast_preview(self, chat_id: int, user_id: int) -> None:
        draft = self._bc_draft.get(user_id)
        if not draft:
            await self.client.send_message(chat_id, "No broadcast draft. "
                                           "Use /broadcast or Settings → Broadcast.")
            return
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Send broadcast", callback_data="bc:send"),
            InlineKeyboardButton("❌ Cancel", callback_data="bc:cancel")]])
        preview = draft["text"] or draft.get("caption") or "[photo]"
        if draft.get("photo"):
            await self.client.send_photo(
                chat_id, draft["photo"], caption=f"📣 <b>Broadcast preview</b>\n\n{preview[:1000]}",
                parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await self.client.send_message(
                chat_id, f"📣 <b>Broadcast preview</b>\n\n{preview}",
                parse_mode=ParseMode.HTML, reply_markup=kb)

    async def _run_broadcast(self, owner_chat: int, draft: dict) -> None:
        log_ch = await run_db(owner_mod.resolve_log_channel, self.db,
                              self.cfg.log_channel_id)
        recipients = await run_db(owner_mod.collect_broadcast_recipients,
                                  self.db, log_ch)
        delivered = failed = 0
        for i, rid in enumerate(recipients):
            try:
                if draft.get("photo"):
                    await self.client.send_photo(rid, draft["photo"],
                                                 caption=draft.get("caption") or "")
                else:
                    await self.client.send_message(rid, draft.get("text") or "")
                delivered += 1
            except Exception as e:
                if type(e).__name__ == "FloodWait":
                    wait = getattr(e, "value", 60) or 60
                    await asyncio.sleep(wait)
                    try:
                        if draft.get("photo"):
                            await self.client.send_photo(rid, draft["photo"],
                                                         caption=draft.get("caption") or "")
                        else:
                            await self.client.send_message(rid, draft.get("text") or "")
                        delivered += 1
                        continue
                    except Exception:
                        pass
                failed += 1
            if i and i % 10 == 0:
                await asyncio.sleep(1)
        try:
            await self.client.send_message(
                owner_chat,
                f"📣 <b>Broadcast done</b> — delivered: <b>{delivered}</b>, "
                f"failed: <b>{failed}</b>, total: <b>{len(recipients)}</b>.",
                parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("broadcast report failed: %r", e)

    async def _owner_callback(self, q: CallbackQuery, data: str) -> None:
        uid, cid = q.from_user.id, q.message.chat.id
        if data == "settings:main":
            await self._edit_or_send(q, await self._settings_text(),
                                     self._settings_kb())
        elif data == "settings:photo":
            photo = await run_db(owner_mod.get_start_photo, self.db)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📸 Send new photo",
                                      callback_data="settings:photo_new")],
                [InlineKeyboardButton("🗑 Remove photo",
                                      callback_data="settings:photo_remove")],
                [InlineKeyboardButton("⬅ Settings", callback_data="settings:main")]])
            await self._edit_or_send(
                q, f"📸 <b>Start photo</b>\n\nCurrently: {'set ✅' if photo else 'not set'}\n\n"
                   "Tap “Send new photo”, then send a photo here (or /cancel).",
                kb)
        elif data == "settings:photo_new":
            self._pending_set(uid, "start_photo")
            await q.answer()
            await self.client.send_message(
                cid, "📸 Send the new start photo now (or /cancel).")
        elif data == "settings:photo_remove":
            await run_db(owner_mod.set_start_photo, self.db, "")
            await q.answer("Start photo removed.")
            await self._edit_or_send(q, await self._settings_text(),
                                     self._settings_kb())
        elif data == "settings:fsub":
            entries = await run_db(owner_mod.fsub_list, self.db)
            rows = [[InlineKeyboardButton("➕ Add channel",
                                          callback_data="settings:fsub_add")]]
            for e in entries:
                rows.append([InlineKeyboardButton(
                    f"🗑 {e.get('title') or e.get('chat_id')}",
                    callback_data=f"fsub:rm:{e['chat_id']}")])
            rows.append([InlineKeyboardButton("⬅ Settings",
                                              callback_data="settings:main")])
            text = ("🔒 <b>Force subscribe</b>\n\n" +
                    ("\n".join(f"• <code>{e['chat_id']}</code> "
                               f"{html.escape(str(e.get('title') or ''))}"
                               for e in entries) if entries
                     else "No channels set — nobody is blocked."))
            await self._edit_or_send(q, text, InlineKeyboardMarkup(rows))
        elif data == "settings:fsub_add":
            self._pending_set(uid, "fsub_add")
            await q.answer()
            await self.client.send_message(
                cid, "🔒 Send a channel @username or -100 id now (or /cancel). "
                     "The bot must be able to access it, and I need an invite "
                     "link (admin) or a username, or I won't store it.")
        elif data.startswith("fsub:rm:"):
            try:
                target = int(data[8:])
            except ValueError:
                await q.answer("Bad channel id.")
                return
            entries = [e for e in await run_db(owner_mod.fsub_list, self.db)
                       if e.get("chat_id") != target]
            await run_db(owner_mod.set_fsub, self.db, entries)
            await q.answer("Removed.")
            await self._owner_callback(q, "settings:fsub")
        elif data == "settings:logs":
            log_ch = await run_db(owner_mod.resolve_log_channel, self.db,
                                  self.cfg.log_channel_id)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Set channel",
                                      callback_data="settings:logs_set"),
                 InlineKeyboardButton("✅ Test", callback_data="log:test")],
                [InlineKeyboardButton("🚫 Disable", callback_data="log:disable")],
                [InlineKeyboardButton("⬅ Settings", callback_data="settings:main")]])
            await self._edit_or_send(
                q, f"📋 <b>Live logs</b>\n\nCurrent: "
                   f"{'<code>' + str(log_ch) + '</code>' if log_ch else 'off'}\n\n"
                   "Every private message from a non-admin user is logged "
                   "there (time, user, text up to 300 chars — never media). "
                   "Keep that channel private.",
                kb)
        elif data == "settings:logs_set":
            self._pending_set(uid, "log_set")
            await q.answer()
            await self.client.send_message(
                cid, "📋 Send the logs channel -100 id now (or /cancel). "
                     "The bot must be admin there, or I won't store it.")
        elif data == "log:test":
            log_ch = await run_db(owner_mod.resolve_log_channel, self.db,
                                  self.cfg.log_channel_id)
            if not log_ch:
                await q.answer("Live logs are off.", show_alert=True)
                return
            try:
                await self.client.send_message(log_ch, "✅ Live logs test — "
                                               "the bot can post here.")
                await q.answer("Test sent ✅")
            except Exception as e:
                await q.answer(f"Test failed: {type(e).__name__}",
                               show_alert=True)
        elif data == "log:disable":
            await run_db(owner_mod.set_log_channel, self.db, 0)
            await q.answer("Live logs disabled.")
            await self._edit_or_send(q, await self._settings_text(),
                                     self._settings_kb())
        elif data == "settings:users":
            text, kb = await self._users_text(0)
            await self._edit_or_send(q, text, kb)
        elif data.startswith("users:"):
            try:
                offset = int(data[6:])
            except ValueError:
                offset = 0
            text, kb = await self._users_text(max(0, offset))
            await self._edit_or_send(q, text, kb)
        elif data == "settings:broadcast":
            self._pending_set(uid, "broadcast")
            await q.answer()
            await self.client.send_message(
                cid, "📣 Send the broadcast message now (text, or a photo with "
                     "a caption). I'll show a preview with Send / Cancel "
                     "before anything goes out. /cancel to abort.")
        elif data == "bc:send":
            draft = self._bc_draft.pop(uid, None)
            if not draft:
                await q.answer("No draft.")
                return
            await q.answer("Broadcasting…")
            try:
                await q.message.edit_text("📣 Broadcasting…")
            except Exception:
                pass
            asyncio.create_task(self._run_broadcast(cid, draft))
        elif data == "bc:cancel":
            self._pending_clear(uid)
            await q.answer("Cancelled.")
            try:
                await q.message.edit_text("Broadcast cancelled.")
            except Exception:
                pass
        else:
            await q.answer()

    async def _handle_pending(self, m: Message, kind: str) -> None:
        uid, cid = m.from_user.id, m.chat.id
        if kind == "start_photo":
            photo = getattr(m, "photo", None)
            file_id = None
            if photo is not None:
                file_id = getattr(photo, "file_id", None)
                if not file_id and isinstance(photo, (list, tuple)) and photo:
                    file_id = getattr(photo[-1], "file_id", None)
            if not file_id:
                await m.reply_text("That's not a photo — send a photo, or /cancel.")
                return
            await run_db(owner_mod.set_start_photo, self.db, file_id)
            self._pending_clear(uid)
            await m.reply_text("✅ Start photo updated. Try /start to see it.")
        elif kind == "fsub_add":
            target = owner_mod.parse_chat_arg(m.text or "")
            if target is None:
                await m.reply_text("Send a channel @username or -100 id, or /cancel.")
                return
            try:
                chat = await self.client.get_chat(target)
            except Exception as e:
                await m.reply_text(f"Can't access that chat: {e}")
                return
            invite = ""
            try:
                invite = await self.client.export_chat_invite_link(chat.id) or ""
            except Exception:
                invite = ""
            username = getattr(chat, "username", "") or ""
            if not invite and not username:
                await m.reply_text(
                    "⚠️ I can't get an invite link for this channel (I'm not "
                    "admin there) and it has no username, so users couldn't "
                    "join it. Not stored — make me admin first, or use a "
                    "public channel, then try again.")
                return
            entries = await run_db(owner_mod.fsub_list, self.db)
            entries = [e for e in entries if e.get("chat_id") != chat.id]
            entries.append({"chat_id": chat.id, "title": chat.title or "",
                            "username": username, "invite_link": invite})
            await run_db(owner_mod.set_fsub, self.db, entries)
            self._pending_clear(uid)
            await m.reply_text(f"✅ Force-subscribe added: "
                               f"<b>{html.escape(chat.title or str(chat.id))}</b> "
                               f"(<code>{chat.id}</code>).",
                               parse_mode=ParseMode.HTML)
        elif kind == "log_set":
            target = owner_mod.parse_chat_arg(m.text or "")
            if not isinstance(target, int):
                await m.reply_text("Send a numeric -100 channel id, or /cancel.")
                return
            try:
                await self.client.get_chat(target)
                me = await self._self_id()
                member = await self.client.get_chat_member(target, me) if me else None
                status = owner_mod._status_name(member) if member else ""
                if status not in ("administrator", "creator"):
                    await m.reply_text(
                        "⚠️ I'm not admin in that channel, so I can't post "
                        "logs there. Not stored — make me admin first.")
                    return
            except Exception as e:
                await m.reply_text(f"Can't verify that channel: {e}")
                return
            await run_db(owner_mod.set_log_channel, self.db, target)
            self._pending_clear(uid)
            await m.reply_text(f"✅ Live logs channel set to <code>{target}</code>.",
                               parse_mode=ParseMode.HTML)
        elif kind == "broadcast":
            text = (m.text or "").strip()
            photo = getattr(m, "photo", None)
            file_id = None
            if photo is not None:
                file_id = getattr(photo, "file_id", None)
                if not file_id and isinstance(photo, (list, tuple)) and photo:
                    file_id = getattr(photo[-1], "file_id", None)
            if not text and not file_id:
                await m.reply_text("Send text or a photo with a caption, or /cancel.")
                return
            self._pending.pop(uid, None)
            self._bc_draft[uid] = {"text": text, "photo": file_id,
                                   "caption": m.caption or ""}
            await self._broadcast_preview(cid, uid)
        else:
            self._pending_clear(uid)

    # -- flows ------------------------------------------------------------------
    async def _do_search(self, chat_id: int, q: str, user_id: int):
        """Plain-text and /search: exactly ONE reply, never a card per result.

        The old version sent one photo message per match (up to 6), which
        flooded the chat. Now the reply is a single message with one Mini App
        button that opens straight on the search results (the query is
        carried in the start_param).
        """
        t0 = time.monotonic()
        now = time.time()
        if now - self._spam.get(user_id, 0) < 1.5:
            return
        self._spam[user_id] = now
        if len(self._spam) > 5000:
            self._spam.clear()
        unjoined = await self._unjoined(user_id)
        if unjoined:
            await self._send_fsub_prompt(chat_id, unjoined)
            return
        rows = await run_db_fast(self.search_titles, q, limit=PAGE + 1)
        if not rows:
            kb = InlineKeyboardMarkup([[self._app_button("🌐 Open Media Library")]])
            await self.client.send_message(
                chat_id, f"No results for <b>{html.escape(q)}</b>.\n"
                         "Try another spelling, or browse the app.",
                parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        shown, more = rows[:PAGE], len(rows) > PAGE
        top = shown[0]
        text = (f"🔎 <b>{html.escape(q)}</b>\n"
                f"{len(shown)}{'+' if more else ''} title"
                f"{'s' if len(shown) != 1 else ''} found\n\n"
                f"Top match: {_title_line(top)}")
        if top["rating"]:
            text += f" ⭐ {top['rating']:.1f}"
        text += ("\n\nOpen the app for posters, all qualities "
                 "and Get / Play.")
        kb_rows = [[self._app_button("🌐 View results in app",
                                     _search_start_param(q))]]
        await self.client.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                       reply_markup=InlineKeyboardMarkup(kb_rows))
        dt = time.monotonic() - t0
        if dt > 10:
            log.warning("slow search reply: %.1fs for user %s q=%r", dt, user_id, q)

    async def _show_seasons(self, q: CallbackQuery, title_id: int):
        t = await run_db_fast(self.db.query_one, "SELECT * FROM titles WHERE id=?",
                              (title_id,))
        seasons = await run_db_fast(
            self.db.query,
            "SELECT * FROM seasons WHERE title_id=? ORDER BY season_no",
            (title_id,))
        rows = [[InlineKeyboardButton(f"Season {s['season_no']}",
                                      callback_data=f"season:{s['id']}")] for s in seasons]
        rows.append([self._app_button("🌐 Open in app", f"title_{title_id}")])
        text = f"{_title_line(t)}\n\nSelect a season:"
        try:
            await q.message.edit_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(rows))
        except Exception:
            pass
        await q.answer()

    async def _show_episodes(self, q: CallbackQuery, season_id: int):
        se = await run_db_fast(self.db.query_one, "SELECT * FROM seasons WHERE id=?",
                               (season_id,))
        t = await run_db_fast(self.db.query_one, "SELECT * FROM titles WHERE id=?",
                              (se["title_id"],))
        eps = await run_db_fast(
            self.db.query,
            "SELECT * FROM episodes WHERE season_id=? ORDER BY ep_no",
            (season_id,))
        rows = []
        packs = await run_db_fast(
            self.db.query,
            "SELECT * FROM files WHERE season_id=? AND episode_id IS NULL "
            "AND status='ok' ORDER BY " + RES_ORDER + " DESC", (season_id,))
        for f in packs:
            label = f"📦 {f['pack_label'] or 'Pack'} ({f['resolution'] or '?'})"
            rows.append([InlineKeyboardButton(label, callback_data=f"getf:{f['id']}")])
        for e in eps:
            n = (await run_db_fast(
                self.db.query_one,
                "SELECT COUNT(*) c FROM files WHERE episode_id=?",
                (e["id"],)))["c"]
            label = f"E{e['ep_no']:02d}" + (f" — {e['ep_title']}" if e["ep_title"] else "")
            rows.append([InlineKeyboardButton(f"{label} ({n} file{'s' if n!=1 else ''})",
                                              callback_data=f"ep:{e['id']}")])
        rows.append([InlineKeyboardButton("⬅ Seasons",
                                          callback_data=f"seasons:{t['id']}")])
        try:
            await q.message.edit_text(
                f"{_title_line(t)} — <b>Season {se['season_no']}</b>\n\nSelect an episode:",
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception:
            pass
        await q.answer()

    async def _show_episode(self, q: CallbackQuery, episode_id: int):
        e = await run_db_fast(self.db.query_one, "SELECT * FROM episodes WHERE id=?",
                              (episode_id,))
        se = await run_db_fast(self.db.query_one, "SELECT * FROM seasons WHERE id=?",
                               (e["season_id"],))
        t = await run_db_fast(self.db.query_one, "SELECT * FROM titles WHERE id=?",
                              (e["title_id"],))
        files = await run_db_fast(self._versions, t["id"], episode_id)
        kb = self._kb_versions(files) or InlineKeyboardMarkup([[]])
        kb.inline_keyboard.append([InlineKeyboardButton(
            "⬅ Episodes", callback_data=f"season:{se['id']}")])
        label = f"E{e['ep_no']:02d}" + (f" — {html.escape(e['ep_title'])}" if e["ep_title"] else "")
        try:
            await q.message.edit_text(
                f"{_title_line(t)} — <b>Season {se['season_no']} {label}</b>\n\n"
                "Choose a quality:", parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            pass
        await q.answer()

    async def deliver_to_user(self, user_id: int, file_id) -> tuple[bool, str]:
        """Copy a catalog file straight into a user's DM.

        Used by the Mini App backend (no deep-link round-trip needed).
        Returns (ok, message). Fails cleanly if the user never started the bot.
        Force-subscribe is checked first (the API checks it too, so this is
        the bot-path net for deep links and callbacks).
        """
        if await self._unjoined(user_id):
            return False, "fsub_required"
        try:
            fid = int(file_id)
        except (ValueError, TypeError):
            return False, "bad file id"
        f = await run_db_fast(self.db.query_one, "SELECT * FROM files WHERE id=?",
                              (fid,))
        if not f:
            return False, "File not found."
        try:
            msg = await self.client.get_messages(f["tg_chat_id"], f["tg_msg_id"])
            if not msg or msg.empty:
                return False, "Source file unavailable."
            caption = f"🎬 <b>{html.escape(f['file_name'])}</b>"
            if f["resolution"]:
                caption += f" · {f['resolution']}"
            sent = await msg.copy(user_id, caption=caption, parse_mode=ParseMode.HTML)
            warn = await self.client.send_message(
                user_id,
                "⏳ The file will be deleted in 10 minutes. "
                "Forward the files to your saved messages.")
            await self._schedule_delete(user_id, [sent.id, warn.id], delay=600)
            return True, "sent"
        except Exception as e:
            log.warning("deliver_to_user failed: %r", e)
            name = type(e).__name__
            if name in ("PeerIdInvalid", "UserIsBlocked", "InputUserDeactivated"):
                return False, "not_started"
            return False, f"{name}: {e}"[:200]

    async def _deliver_file(self, chat_id: int, file_id: str, user_id: int):
        ok, msg = await self.deliver_to_user(chat_id, file_id)
        if not ok:
            if msg == "fsub_required":
                unjoined = await self._unjoined(user_id)
                await self._send_fsub_prompt(chat_id, unjoined)
                return
            if msg == "not_started":
                text = ("👋 Press <b>START</b> below first, then tap Get again.")
            elif msg in ("File not found.", "Source file unavailable."):
                text = msg
            else:
                text = ("Couldn't fetch that file — the bot may not be admin "
                        "in the source channel.")
            await self.client.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    # -- self-destructing deliveries -----------------------------------------
    async def _schedule_delete(self, chat_id: int, msg_ids: list[int], delay: float):
        """Delete messages after `delay` seconds. Survives restarts via DB."""
        delete_at = time.time() + delay
        for mid in msg_ids:
            await run_db(self.db.add_pending_delete, chat_id, mid, delete_at)
        asyncio.create_task(self._auto_delete(chat_id, msg_ids, delay))

    async def _auto_delete(self, chat_id: int, msg_ids: list[int], delay: float):
        await asyncio.sleep(delay)
        try:
            await self.client.delete_messages(chat_id, msg_ids)
        except Exception as e:
            log.warning("auto-delete failed: %r", e)
        for mid in msg_ids:
            await run_db(self.db.remove_pending_delete, chat_id, mid)

    async def _recover_pending_deletes(self):
        """Re-schedule deletions that were pending when the bot restarted."""
        for row in await run_db(self.db.list_pending_deletes):
            delay = max(0.0, row["delete_at"] - time.time())
            asyncio.create_task(
                self._auto_delete(row["chat_id"], [row["msg_id"]], delay))

    async def _prompt_index(self, notify_chat: int, chat_id, top_id: int, title: str):
        """Access-test the channel, then ask the admin to confirm indexing."""
        ok, reason = await self.indexer.access_test(chat_id)
        name = html.escape(title)
        if not ok:
            await self.client.send_message(
                notify_chat,
                f"⚠️ Can't index <b>{name}</b> — {html.escape(reason)}",
                parse_mode=ParseMode.HTML)
            return
        try:
            real = await self.client.get_chat(chat_id)
            cid = real.id
            title = real.title or title
            username = real.username or ""
        except Exception:
            cid, username = chat_id, ""
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, index it",
                                 callback_data=f"idx_yes:{cid}:{top_id}"),
            InlineKeyboardButton("❌ No", callback_data="idx_no"),
        ]])
        await run_db(self.db.upsert_source, cid, title, username)
        await self.client.send_message(
            notify_chat,
            f"📡 Index <b>{html.escape(title)}</b> (<code>{cid}</code>)?\n"
            f"Last message: <code>{top_id}</code> — I'll scan the full history "
            f"and keep indexing new posts.",
            parse_mode=ParseMode.HTML, reply_markup=kb)

    async def _index_yes(self, q: CallbackQuery, chat_id: int, top_id: int):
        chat = await run_db(self.db.query_one,
                            "SELECT title FROM sources WHERE chat_id=?", (chat_id,))
        title = (chat["title"] if chat else None) or str(chat_id)
        queued = self.indexer._scan_lock.locked()
        await q.answer("Indexing queued — starts when the current scan finishes."
                       if queued else "Indexing started!")
        try:
            await q.message.edit_text(
                f"✅ Indexing <b>{html.escape(title)}</b> from message 1 to {top_id}…\n"
                + ("⏳ Another scan is running — yours is queued and starts "
                   "automatically when it finishes.\n" if queued else "") +
                f"I'll ping you when the scan finishes.",
                parse_mode=ParseMode.HTML)
        except Exception:
            pass
        task = asyncio.create_task(self._scan_with_report(q.message.chat.id, chat_id,
                                                          title, top_id))
        task.add_done_callback(
            lambda t: self._scan_task_done(t, q.message.chat.id, title))

    def _scan_task_done(self, task: asyncio.Task, notify_chat: int, name: str) -> None:
        """Last-resort net: a scan task must never die silently."""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            log.error("scan task for %s crashed: %r", name, exc)
            try:
                coro = self.client.send_message(
                    notify_chat,
                    f"⚠️ Scan of <b>{html.escape(name)}</b> crashed unexpectedly: "
                    f"<code>{html.escape(str(exc)[:200])}</code>\n"
                    "It will be retried automatically.",
                    parse_mode=ParseMode.HTML)
                asyncio.create_task(coro)
            except Exception:
                pass

    async def _scan_with_report(self, notify_chat: int, chat_id: int, name: str,
                                top_id: int = 0):
        status_msg = None
        last_edit = 0.0

        def _progress(done: int, total: int):
            # called from the scan loop; schedule a throttled progress edit
            nonlocal last_edit
            now = time.time()
            if now - last_edit < 30 and done < total:
                return
            last_edit = now
            if status_msg is not None:
                pct = int(done * 100 / total) if total else 0
                coro = status_msg.edit_text(
                    f"⏳ Indexing <b>{html.escape(name)}</b>… "
                    f"{done:,}/{total:,} messages ({pct}%)",
                    parse_mode=ParseMode.HTML)
                asyncio.create_task(coro)

        try:
            status_msg = await self.client.send_message(
                notify_chat, f"⏳ Indexing <b>{html.escape(name)}</b>…",
                parse_mode=ParseMode.HTML)
        except Exception:
            status_msg = None
        res = await self.indexer.scan_source(chat_id, top_id=top_id,
                                             progress_cb=_progress)
        try:
            if res.get("error"):
                if res.get("note") == "busy":
                    text = (f"⏳ <b>{html.escape(name)}</b>: another scan is still "
                            f"running — this one will start automatically.")
                else:
                    text = (f"⚠️ Scan of <b>{html.escape(name)}</b> hit an error: "
                            f"{html.escape(str(res['error'])[:200])}\n"
                            f"Checked {res.get('scanned', 0):,} messages, "
                            f"indexed {res.get('new', 0):,} new files. "
                            f"It will resume automatically.")
            else:
                skipped = res.get("poison_skipped", 0)
                skip_note = (f" ({skipped} unresolvable message ranges skipped — "
                             f"Telegram never answered for them)") if skipped else ""
                stopped_note = (" — 🛑 stopped by admin, checkpoint kept; "
                                "it won't resume on its own.") if res.get("note") == "stopped by admin" else ""
                text = (f"✅ Scan of <b>{html.escape(name)}</b> done — "
                        f"{res.get('scanned', 0):,} messages checked, "
                        f"{res.get('new', 0):,} new files indexed.{skip_note}{stopped_note}")
            if status_msg is not None:
                try:
                    await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
                    return
                except Exception:
                    pass
            await self.client.send_message(notify_chat, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("scan report failed: %r", e)

    async def _reparse_report(self, notify_chat: int):
        try:
            res = await self.indexer.rebuild_catalog()
            text = (f"✅ Re-parse done: <b>{res['files']}</b> files regrouped into "
                    f"<b>{res['titles']}</b> titles.\nTMDB posters will fill in over "
                    f"the next few minutes.")
        except Exception as e:
            log.warning("reparse failed: %r", e)
            text = f"⚠️ Re-parse failed: {html.escape(str(e)[:200])}"
        try:
            await self.client.send_message(notify_chat, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("reparse report failed: %r", e)

    async def _cleanup_report(self, notify_chat: int):
        try:
            res = await self.indexer.cleanup_catalog()
            text = (f"✅ Cleanup done: <b>{res['moved']:,}</b> files moved to their "
                    f"correct titles, <b>{res['orphans_removed']:,}</b> empty junk "
                    f"titles removed, <b>{res['kept']:,}</b> files already correct.")
            if res.get("resumed"):
                text += ("\n↩️ This run picked up from the saved checkpoint "
                         "instead of starting over.")
        except Exception as e:
            log.warning("cleanup failed: %r", e)
            text = f"⚠️ Cleanup failed: {html.escape(str(e)[:200])}"
        try:
            await self.client.send_message(notify_chat, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("cleanup report failed: %r", e)

    async def _index_all_report(self, notify_chat: int):
        lines = []
        for src in await run_db(self.db.list_sources):
            if not src["enabled"]:
                continue
            res = await self.indexer.scan_source(src["chat_id"])
            name = html.escape(src["title"] or str(src["chat_id"]))
            if res.get("error"):
                lines.append(f"⚠️ {name}: {html.escape(str(res['error'])[:200])}")
            else:
                lines.append(f"✅ {name}: {res.get('scanned', 0)} checked, "
                             f"{res.get('new', 0)} new files indexed")
        try:
            await self.client.send_message(
                notify_chat, "\n".join(lines) or "No sources configured.",
                parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("index report failed: %r", e)

    async def start(self):
        await self.client.start()
        me = await self.client.get_me()
        self.username = me.username or ""
        log.info("bot @%s started", self.username)
        await self._set_menu_button()
        asyncio.create_task(self._recover_pending_deletes())

    async def stop(self):
        try:
            await self.client.stop()
        except Exception:
            pass
