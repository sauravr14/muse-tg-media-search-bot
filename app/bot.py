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

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
import aiohttp

from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message, WebAppInfo)

from .config import Config
from .db import DB, RES_ORDER
from .dbthread import run_db
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
        self.client = Client("mediabot", api_id=cfg.api_id, api_hash=cfg.api_hash,
                             bot_token=cfg.bot_token, in_memory=True)
        self.username = ""
        self.panel = None  # IndexPanel, attached in main.py once indexer exists
        self._spam: dict[int, float] = {}
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

    # -- handlers ---------------------------------------------------------------
    def _register(self):
        c = self.client
        admin_ids = list(self.cfg.admin_ids)

        @c.on_message(filters.private & filters.command("start"))
        async def start(_, m: Message):
            arg = (m.text or "").split(maxsplit=1)
            if len(arg) > 1 and arg[1].startswith("file_"):
                await self._deliver_file(m.chat.id, arg[1][5:], m.from_user.id)
                return
            kb = InlineKeyboardMarkup([[self._app_button("🌐 Open Media Library")]])
            await m.reply_text(
                "🎬 <b>Media Library</b>\n\n"
                "Search movies & series from our channels.\n"
                "Just type a name, or use:\n"
                "/search <name> · /recent · /movies · /series · /help",
                parse_mode=ParseMode.HTML, reply_markup=kb)

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
                "/reparse · /setskip · /stats",
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
            rows = await run_db(
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
            kind = "movie" if m.command[0] == "movies" else "series"
            rows = await run_db(
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
        @c.on_message(filters.private & is_forwarded & filters.user(admin_ids))
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

        @c.on_message(filters.private & filters.text & filters.user(admin_ids)
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
        # live indexing of new channel posts
        @c.on_message(filters.channel)
        async def live(_, m: Message):
            await self.indexer.on_channel_post(_, m)

    def _register_admin(self, c):
        admin_ids = list(self.cfg.admin_ids)

        @c.on_message(filters.private & filters.command("addchannel") & filters.user(admin_ids))
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

        @c.on_message(filters.private & filters.command("channels") & filters.user(admin_ids))
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

        @c.on_message(filters.private & filters.command("index") & filters.user(admin_ids))
        async def index(_, m: Message):
            await m.reply_text("🔄 Full scan started in the background — "
                               "I'll report here when it's done.")
            asyncio.create_task(self._index_all_report(m.chat.id))

        @c.on_message(filters.private & filters.command("indexstatus") & filters.user(admin_ids))
        async def indexstatus(_, m: Message):
            if self.panel:
                await self.panel.show(m)
            else:
                await m.reply_text("Panel not ready yet — try again in a few seconds.")

        @c.on_message(filters.private & filters.command("stopindex") & filters.user(admin_ids))
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

        @c.on_message(filters.private & filters.command("setskip") & filters.user(admin_ids))
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

        @c.on_message(filters.private & filters.command("stats") & filters.user(admin_ids))
        async def stats(_, m: Message):
            s = await run_db(self.db.stats)
            await m.reply_text(
                f"📊 <b>Catalog stats</b>\n"
                f"Files: <b>{s['files']}</b> · Movies: <b>{s['movies']}</b> · "
                f"Series: <b>{s['series']}</b> · Episodes: <b>{s['episodes']}</b>\n"
                f"Sources: <b>{s['sources']}</b> · Unmatched: <b>{s['unmatched']}</b>",
                parse_mode=ParseMode.HTML)

    # -- flows ------------------------------------------------------------------
    async def _do_search(self, chat_id: int, q: str, user_id: int):
        """Plain-text and /search: exactly ONE reply, never a card per result.

        The old version sent one photo message per match (up to 6), which
        flooded the chat. Now the reply is a single message with one Mini App
        button that opens straight on the search results (the query is
        carried in the start_param).
        """
        now = time.time()
        if now - self._spam.get(user_id, 0) < 1.5:
            return
        self._spam[user_id] = now
        rows = await run_db(self.search_titles, q, limit=PAGE + 1)
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

    async def _show_seasons(self, q: CallbackQuery, title_id: int):
        t = await run_db(self.db.query_one, "SELECT * FROM titles WHERE id=?",
                         (title_id,))
        seasons = await run_db(
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
        se = await run_db(self.db.query_one, "SELECT * FROM seasons WHERE id=?",
                          (season_id,))
        t = await run_db(self.db.query_one, "SELECT * FROM titles WHERE id=?",
                         (se["title_id"],))
        eps = await run_db(
            self.db.query,
            "SELECT * FROM episodes WHERE season_id=? ORDER BY ep_no",
            (season_id,))
        rows = []
        packs = await run_db(
            self.db.query,
            "SELECT * FROM files WHERE season_id=? AND episode_id IS NULL "
            "AND status='ok' ORDER BY " + RES_ORDER + " DESC", (season_id,))
        for f in packs:
            label = f"📦 {f['pack_label'] or 'Pack'} ({f['resolution'] or '?'})"
            rows.append([InlineKeyboardButton(label, callback_data=f"getf:{f['id']}")])
        for e in eps:
            n = (await run_db(
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
        e = await run_db(self.db.query_one, "SELECT * FROM episodes WHERE id=?",
                         (episode_id,))
        se = await run_db(self.db.query_one, "SELECT * FROM seasons WHERE id=?",
                          (e["season_id"],))
        t = await run_db(self.db.query_one, "SELECT * FROM titles WHERE id=?",
                         (e["title_id"],))
        files = await run_db(self._versions, t["id"], episode_id)
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
        """
        try:
            fid = int(file_id)
        except (ValueError, TypeError):
            return False, "bad file id"
        f = await run_db(self.db.query_one, "SELECT * FROM files WHERE id=?",
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
