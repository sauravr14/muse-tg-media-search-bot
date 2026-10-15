"""Telegram bot: search, browse, deep links, file delivery, admin commands.

Runs on the bot token (PyroFork). Plain text in private chat = search.
File delivery copies the source channel message into the user's chat —
the bot must be admin in every source channel.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
import aiohttp

from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message, WebAppInfo)

from .config import Config
from .db import DB, RES_ORDER, _uj
from .indexer import Indexer

log = logging.getLogger("mediabot.bot")
PAGE = 6


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


class MediaBot:
    def __init__(self, cfg: Config, db: DB, indexer: Indexer):
        self.cfg = cfg
        self.db = db
        self.indexer = indexer
        self.client = Client("mediabot", api_id=cfg.api_id, api_hash=cfg.api_hash,
                             bot_token=cfg.bot_token, in_memory=True)
        self.username = ""
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
            "SELECT * FROM files WHERE title_id=? AND episode_id IS NULL AND status='ok' "
            "ORDER BY " + RES_ORDER + " DESC", (title_id,))

    def _kb_versions(self, files, prefix="getf"):
        rows = []
        for f in files:
            label = f"{f['resolution'] or '?'} · {_fmt_size(f['size'])}"
            rows.append([InlineKeyboardButton(f"📥 {label}",
                                              callback_data=f"{prefix}:{f['id']}")])
        return InlineKeyboardMarkup(rows) if rows else None

    async def _send_title_card(self, chat_id: int, t, with_versions: bool = True):
        overview = (t["overview"] or "")[:300]
        text = f"{_title_line(t)}\n"
        if t["rating"]:
            text += f"⭐ {t['rating']:.1f}\n"
        genres = _uj(t["genres"], [])
        if genres:
            text += f"<i>{html.escape(', '.join(genres[:4]))}</i>\n"
        if overview:
            text += f"\n{html.escape(overview)}"
        kb_rows = []
        if t["kind"] == "movie" and with_versions:
            files = self._versions(t["id"])
            kb = self._kb_versions(files)
            if kb:
                kb_rows = kb.inline_keyboard
        elif t["kind"] == "series":
            kb_rows = [[InlineKeyboardButton("📂 Browse seasons",
                                             callback_data=f"seasons:{t['id']}")]]
        kb_rows.append([self._app_button("🌐 Open in app", f"title_{t['id']}")])
        kb = InlineKeyboardMarkup(kb_rows)
        if t["poster"]:
            try:
                await self.client.send_photo(chat_id, t["poster"], caption=text,
                                            parse_mode=ParseMode.HTML, reply_markup=kb)
                return
            except Exception:
                pass
        await self.client.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                       reply_markup=kb)


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
                "Results group all qualities (480p/720p/1080p) under one title. "
                "Tap a quality button to get the file.",
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
            rows = self.db.query(
                "SELECT * FROM titles ORDER BY last_file_at DESC LIMIT 8")
            if not rows:
                await m.reply_text("Nothing indexed yet.")
                return
            await m.reply_text("<b>🆕 Recently added</b>", parse_mode=ParseMode.HTML)
            for t in rows:
                await self._send_title_card(m.chat.id, t, with_versions=False)

        @c.on_message(filters.private & filters.command(["movies", "series"]))
        async def browse(_, m: Message):
            kind = "movie" if m.command[0] == "movies" else "series"
            rows = self.db.query(
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
                    await self._index_yes(q, int(data[8:]))
                elif data.startswith("idx_no:"):
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

        # ---- forward a channel post to index it ----
        @c.on_message(filters.private & filters.forwarded & filters.user(admin_ids))
        async def forward_index(_, m: Message):
            fwd = m.forward_from_chat
            if not fwd:
                return
            try:
                me = await self.client.get_chat_member(fwd.id, "me")
                is_admin = getattr(me, "status", "") in ("administrator", "creator")
            except Exception:
                is_admin = False
            name = html.escape(fwd.title or str(fwd.id))
            if not is_admin:
                await m.reply_text(
                    f"I'm not admin in <b>{name}</b> — make me admin there first, "
                    f"then forward again.", parse_mode=ParseMode.HTML)
                return
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, index it", callback_data=f"idx_yes:{fwd.id}"),
                InlineKeyboardButton("❌ No", callback_data=f"idx_no:{fwd.id}"),
            ]])
            await m.reply_text(
                f"📡 Index <b>{name}</b> (<code>{fwd.id}</code>)?\n"
                f"I'll scan its full history and keep indexing new posts.",
                parse_mode=ParseMode.HTML, reply_markup=kb)

        # ---- admin ----
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
            self.db.upsert_source(chat.id, chat.title or "", chat.username or "")
            await m.reply_text(f"✅ Added <b>{html.escape(chat.title or arg)}</b> "
                               f"(<code>{chat.id}</code>). Run /index to scan history.",
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
            self.db.remove_source(cid)
            await m.reply_text(f"Removed <code>{cid}</code>.", parse_mode=ParseMode.HTML)

        @c.on_message(filters.private & filters.command("channels") & filters.user(admin_ids))
        async def channels(_, m: Message):
            rows = self.db.list_sources()
            if not rows:
                await m.reply_text("No sources configured.")
                return
            lines = ["<b>📡 Sources</b>"]
            for s in rows:
                n = self.db.query_one("SELECT COUNT(*) c FROM files WHERE tg_chat_id=?",
                                      (s["chat_id"],))["c"]
                icon = "🟢" if s["enabled"] else "⚪"
                lines.append(f"{icon} {html.escape(s['title'] or str(s['chat_id']))} "
                             f"— {n} files — {s['status']}")
            await m.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

        @c.on_message(filters.private & filters.command("index") & filters.user(admin_ids))
        async def index(_, m: Message):
            await m.reply_text("🔄 Full scan started in the background — "
                               "I'll report here when it's done.")
            asyncio.create_task(self._index_all_report(m.chat.id))

        @c.on_message(filters.private & filters.command("stats") & filters.user(admin_ids))
        async def stats(_, m: Message):
            s = self.db.stats()
            await m.reply_text(
                f"📊 <b>Catalog stats</b>\n"
                f"Files: <b>{s['files']}</b> · Movies: <b>{s['movies']}</b> · "
                f"Series: <b>{s['series']}</b> · Episodes: <b>{s['episodes']}</b>\n"
                f"Sources: <b>{s['sources']}</b> · Unmatched: <b>{s['unmatched']}</b>",
                parse_mode=ParseMode.HTML)

    # -- flows ------------------------------------------------------------------
    async def _do_search(self, chat_id: int, q: str, user_id: int):
        now = time.time()
        if now - self._spam.get(user_id, 0) < 1.5:
            return
        self._spam[user_id] = now
        rows = self.search_titles(q, limit=6)
        if not rows:
            await self.client.send_message(
                chat_id, f"No results for <b>{html.escape(q)}</b>.\nTry another spelling.",
                parse_mode=ParseMode.HTML)
            return
        for t in rows:
            await self._send_title_card(chat_id, t)

    async def _show_seasons(self, q: CallbackQuery, title_id: int):
        t = self.db.query_one("SELECT * FROM titles WHERE id=?", (title_id,))
        seasons = self.db.query("SELECT * FROM seasons WHERE title_id=? ORDER BY season_no",
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
        se = self.db.query_one("SELECT * FROM seasons WHERE id=?", (season_id,))
        t = self.db.query_one("SELECT * FROM titles WHERE id=?", (se["title_id"],))
        eps = self.db.query("SELECT * FROM episodes WHERE season_id=? ORDER BY ep_no",
                            (season_id,))
        rows = []
        for e in eps:
            n = self.db.query_one("SELECT COUNT(*) c FROM files WHERE episode_id=?",
                                  (e["id"],))["c"]
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
        e = self.db.query_one("SELECT * FROM episodes WHERE id=?", (episode_id,))
        se = self.db.query_one("SELECT * FROM seasons WHERE id=?", (e["season_id"],))
        t = self.db.query_one("SELECT * FROM titles WHERE id=?", (e["title_id"],))
        files = self._versions(t["id"], episode_id)
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

    async def _deliver_file(self, chat_id: int, file_id: str, user_id: int):
        try:
            fid = int(file_id)
        except ValueError:
            return
        f = self.db.query_one("SELECT * FROM files WHERE id=?", (fid,))
        if not f:
            await self.client.send_message(chat_id, "File not found.")
            return
        try:
            msg = await self.client.get_messages(f["tg_chat_id"], f["tg_msg_id"])
            if not msg or msg.empty:
                await self.client.send_message(chat_id, "Source file unavailable.")
                return
            caption = f"🎬 <b>{html.escape(f['file_name'])}</b>"
            if f["resolution"]:
                caption += f" · {f['resolution']}"
            await msg.copy(chat_id, caption=caption, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("deliver failed: %r", e)
            await self.client.send_message(
                chat_id, "Couldn't fetch that file — the bot may not be admin "
                         "in the source channel.")

    async def _index_yes(self, q: CallbackQuery, chat_id: int):
        try:
            chat = await self.client.get_chat(chat_id)
        except Exception as e:
            await q.answer(f"Can't access it: {e}", show_alert=True)
            return
        self.db.upsert_source(chat_id, chat.title or "", chat.username or "")
        await q.answer("Indexing started!")
        try:
            await q.message.edit_text(
                f"✅ Indexing <b>{html.escape(chat.title or str(chat_id))}</b>…\n"
                f"I'll ping you when the history scan finishes.",
                parse_mode=ParseMode.HTML)
        except Exception:
            pass
        asyncio.create_task(self._scan_with_report(q.message.chat.id, chat_id,
                                                   chat.title or str(chat_id)))

    async def _scan_with_report(self, notify_chat: int, chat_id: int, name: str):
        res = await self.indexer.scan_source(chat_id)
        try:
            if res.get("error"):
                text = f"⚠️ Scan of <b>{html.escape(name)}</b> hit an error: {html.escape(str(res['error'])[:200])}"
            else:
                text = (f"✅ Scan of <b>{html.escape(name)}</b> done — "
                        f"{res.get('scanned', 0)} messages checked, "
                        f"{res.get('new', 0)} new files indexed.")
            await self.client.send_message(notify_chat, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("scan report failed: %r", e)

    async def _index_all_report(self, notify_chat: int):
        lines = []
        for src in self.db.list_sources():
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

    async def stop(self):
        try:
            await self.client.stop()
        except Exception:
            pass
