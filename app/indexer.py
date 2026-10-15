"""Channel indexing service (bot-token MTProto, no user session needed).

The bot must be admin (or member) in each source channel. Two paths feed the
catalog:
  1. Historical scan: walks get_chat_history backwards with a persisted
     checkpoint (oldest scanned message id). Resumable and idempotent.
  2. Live handler: new channel posts are indexed as they arrive.

TMDB matching is deferred to a background enricher so a slow or missing
TMDB key never blocks indexing or searching.
"""
from __future__ import annotations

import asyncio
import logging
import time

from pyrogram.errors import FloodWait

from .classify import Classifier
from .db import DB
from .parser import ParsedName, parse_filename
from .tmdb import TMDB

log = logging.getLogger("mediabot.indexer")

MEDIA_TYPES = ("video", "document")
BATCH = 100


def _file_meta_from_msg(msg) -> dict | None:
    media = None
    for attr in MEDIA_TYPES:
        media = getattr(msg, attr, None)
        if media:
            break
    if media is None:
        return None
    name = getattr(media, "file_name", None) or ""
    if not name:
        # fall back to caption's first line, else a synthetic name
        cap = (msg.caption or "").strip().split("\n")[0][:80]
        name = cap if cap else f"file_{msg.id}"
        if "." not in name:
            name += ".mp4" if getattr(msg, "video", None) else ".mkv"
    return {
        "file_name": name,
        "size": getattr(media, "file_size", 0) or 0,
        "duration": getattr(media, "duration", 0) or 0,
        "mime": getattr(media, "mime_type", "") or "",
        "tg_chat_id": msg.chat.id,
        "tg_msg_id": msg.id,
        "caption": msg.caption or "",
        "msg_date": msg.date.timestamp() if getattr(msg, "date", None) else 0,
    }


class Indexer:
    def __init__(self, client, db: DB, tmdb: TMDB):
        # Bot-token only. History is read with messages.GetMessages by explicit
        # ID ranges (like Levii/EvaMaria filter bots) because Telegram blocks
        # bots from messages.GetHistory. The bot must be admin in private
        # channels; public channels work from a forwarded post or t.me link.
        self.client = client
        self.db = db
        self.tmdb = tmdb
        self.clf = Classifier(db)
        self._scan_lock = asyncio.Lock()
        self._enrich_queue: asyncio.Queue[int] = asyncio.Queue()
        self._running = False

    # -- ingestion ---------------------------------------------------------
    async def ingest_message(self, msg) -> dict | None:
        meta = _file_meta_from_msg(msg)
        if meta is None:
            return None
        p = parse_filename(meta["file_name"])
        if not p.title:
            return None
        res = self.clf.ingest(p, meta)
        if not res.get("duplicate"):
            title = self.db.query_one("SELECT id, tmdb_id FROM titles WHERE id=?",
                                      (res["title_id"],))
            if title and not title["tmdb_id"]:
                self._enrich_queue.put_nowait(title["id"])
        return res

    # -- historical scan -----------------------------------------------------
    def max_seen(self, chat_id: int) -> int:
        v = self.db.get_kv(f"max_seen:{chat_id}")
        return int(v) if v.isdigit() else 0

    def note_seen(self, chat_id: int, msg_id: int) -> None:
        if msg_id > self.max_seen(chat_id):
            self.db.set_kv(f"max_seen:{chat_id}", str(msg_id))

    async def access_test(self, chat_id) -> tuple[bool, str]:
        """Single GetMessages probe. Returns (ok, reason)."""
        try:
            m = await self.client.get_messages(chat_id, 1)
            _ = m.empty  # touch it
            return True, ""
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            return await self.access_test(chat_id)
        except Exception as e:
            name = type(e).__name__
            if name in ("ChannelInvalid", "ChannelPrivate", "UsernameInvalid",
                        "UsernameNotModified", "PeerIdInvalid"):
                return False, ("I can't open that channel — make me an admin there "
                               "first (required for private channels).")
            return False, f"{name}: {e}"

    async def scan_source(self, chat_id: int, top_id: int = 0) -> dict:
        """Scan message IDs (checkpoint+1 .. top_id) in batches via GetMessages.

        top_id comes from a forwarded post or t.me link. Falls back to the
        highest ID seen via live posts/forwards. Resumable and idempotent.
        """
        async with self._scan_lock:
            src = self.db.query_one("SELECT * FROM sources WHERE chat_id=?", (chat_id,))
            if not src:
                return {"error": "unknown source"}
            if top_id:
                self.note_seen(chat_id, top_id)
            hi = top_id or self.max_seen(chat_id)
            lo = (src["last_msg_id"] or 0) + 1
            if hi < lo:
                return {"scanned": 0, "new": 0, "note": "nothing new to scan"}
            self.db.set_source(chat_id, status="scanning", error="")
            ok, reason = await self.access_test(chat_id)
            if not ok:
                self.db.set_source(chat_id, status="error", error=reason)
                return {"scanned": 0, "new": 0, "error": reason}
            done = new = 0
            cur = lo
            try:
                while cur <= hi:
                    ids = list(range(cur, min(cur + BATCH - 1, hi) + 1))
                    try:
                        msgs = await self.client.get_messages(chat_id, ids)
                    except FloodWait as e:
                        await asyncio.sleep(e.value + 1)
                        continue
                    for msg in msgs:
                        done += 1
                        if msg is None or getattr(msg, "empty", False):
                            continue
                        r = await self.ingest_message(msg)
                        if r and not r.get("duplicate"):
                            new += 1
                    cur += BATCH
                    self.db.set_source(chat_id, last_msg_id=min(cur - 1, hi))
                    await asyncio.sleep(0.4)
            except Exception as e:
                log.warning("scan of %s failed: %r", chat_id, e)
                self.db.set_source(chat_id, status="error",
                                   error=f"{type(e).__name__}: {e}")
                return {"scanned": done, "new": new, "error": str(e)}
            self.db.set_source(chat_id, status="idle", last_msg_id=hi)
            return {"scanned": done, "new": new}

    async def scan_all(self) -> None:
        for src in self.db.list_sources():
            if src["enabled"]:
                await self.scan_source(src["chat_id"])

    async def rebuild_catalog(self) -> dict:
        """Re-parse every stored file with the current parser and regroup
        titles/seasons/episodes from scratch. Telegram references (chat/msg
        ids) are preserved, so no re-scan is needed; hidden flags survive.
        New titles are queued for TMDB enrichment."""
        from .parser import parse_filename
        async with self._scan_lock:
            files = self.db.query("SELECT * FROM files")
            hidden = {(f["tg_chat_id"], f["tg_msg_id"])
                      for f in files if f["status"] == "hidden"}
            self.db.execute("DELETE FROM files")
            self.db.execute("DELETE FROM episodes")
            self.db.execute("DELETE FROM seasons")
            self.db.execute("DELETE FROM titles")
            n = 0
            for f in files:
                p = parse_filename(f["file_name"] or "")
                if not p.title:
                    continue
                meta = {"file_name": f["file_name"], "size": f["size"] or 0,
                        "duration": f["duration"] or 0, "mime": f["mime"] or "",
                        "tg_chat_id": f["tg_chat_id"], "tg_msg_id": f["tg_msg_id"],
                        "caption": f["caption"] or "", "msg_date": f["msg_date"] or 0}
                res = self.clf.ingest(p, meta)
                n += 1
                if res.get("duplicate"):
                    continue
                if (f["tg_chat_id"], f["tg_msg_id"]) in hidden:
                    self.db.execute("UPDATE files SET status='hidden' WHERE id=?",
                                    (res["file_id"],))
                title = self.db.query_one(
                    "SELECT id, tmdb_id FROM titles WHERE id=?", (res["title_id"],))
                if title and not title["tmdb_id"]:
                    try:
                        self._enrich_queue.put_nowait(title["id"])
                    except asyncio.QueueFull:
                        pass
            titles = self.db.query_one("SELECT COUNT(*) c FROM titles")["c"]
            log.info("catalog rebuilt: %d files -> %d titles", n, titles)
            return {"files": n, "titles": titles}

    # -- live handler ----------------------------------------------------------
    async def on_channel_post(self, _, msg) -> None:
        src = self.db.query_one(
            "SELECT id FROM sources WHERE chat_id=? AND enabled=1", (msg.chat.id,)
        )
        if not src:
            return
        try:
            self.note_seen(msg.chat.id, msg.id)
            await self.ingest_message(msg)
        except Exception as e:
            log.warning("live ingest failed: %r", e)

    # -- TMDB enricher -----------------------------------------------------------
    async def enricher_loop(self) -> None:
        """Background: attach TMDB metadata to unmatched titles."""
        self._running = True
        while self._running:
            try:
                title_id = await asyncio.wait_for(self._enrich_queue.get(), timeout=5)
            except asyncio.TimeoutError:
                # also sweep old unmatched titles slowly
                row = self.db.query_one(
                    "SELECT id FROM titles WHERE tmdb_id IS NULL ORDER BY added_at LIMIT 1"
                )
                if row and not self._recently_tried(row["id"]):
                    title_id = row["id"]
                else:
                    continue
            await self._enrich_one(title_id)
            await asyncio.sleep(1.0)

    def _recently_tried(self, title_id: int) -> bool:
        ts = self.db.get_kv(f"tmdb_try:{title_id}")
        return bool(ts) and (time.time() - float(ts) < 7 * 24 * 3600)

    async def _enrich_one(self, title_id: int) -> None:
        row = self.db.query_one("SELECT * FROM titles WHERE id=?", (title_id,))
        if not row or row["tmdb_id"]:
            return
        self.db.set_kv(f"tmdb_try:{title_id}", str(time.time()))
        p = ParsedName(raw=row["title"], title=row["title"],
                       norm_title=row["norm_title"], year=row["year"], kind=row["kind"])
        meta = await self.tmdb.match(p)
        if not meta:
            return
        # adopt canonical TMDB title; merge possible duplicate title rows
        await self._adopt_meta(row, meta)

    async def _adopt_meta(self, row, meta: dict) -> None:
        from .parser import normalize_title
        db = self.db
        now = time.time()
        # if another title already owns this tmdb_id, merge into it
        other = db.query_one("SELECT id FROM titles WHERE tmdb_id=? AND id != ?",
                             (meta["tmdb_id"], row["id"]))
        if other:
            db.execute("UPDATE files SET title_id=? WHERE title_id=?", (other["id"], row["id"]))
            db.execute("UPDATE episodes SET title_id=? WHERE title_id=?", (other["id"], row["id"]))
            db.execute("DELETE FROM seasons WHERE title_id=?", (row["id"],))
            db.execute("DELETE FROM titles WHERE id=?", (row["id"],))
            db.execute("UPDATE titles SET last_file_at=? WHERE id=?", (now, other["id"]))
            log.info("merged title %s into tmdb %s", row["id"], meta["tmdb_id"])
            return
        db.execute(
            "UPDATE titles SET title=?, norm_title=?, orig_title=?, alt_titles=?, year=?,"
            " tmdb_id=?, poster=?, backdrop=?, overview=?, genres=?, rating=?,"
            " popularity=?, lang=?, updated_at=? WHERE id=?",
            (meta["title"], normalize_title(meta["title"]), meta.get("orig_title", ""),
             __import__("json").dumps(meta.get("alt_titles", [])),
             meta.get("year") or row["year"], meta["tmdb_id"], meta.get("poster", ""),
             meta.get("backdrop", ""), meta.get("overview", ""),
             __import__("json").dumps(meta.get("genres", [])),
             meta.get("rating"), meta.get("popularity"), meta.get("lang", ""), now, row["id"]),
        )
        # fill episode titles for series
        if meta["kind"] == "series" and meta.get("tmdb_id"):
            for se in db.query("SELECT * FROM seasons WHERE title_id=?", (row["id"],)):
                for ep in db.query("SELECT * FROM episodes WHERE season_id=?", (se["id"],)):
                    if not ep["ep_title"]:
                        name = await self.tmdb.episode_title(
                            meta["tmdb_id"], se["season_no"], ep["ep_no"])
                        if name:
                            db.execute("UPDATE episodes SET ep_title=? WHERE id=?",
                                       (name, ep["id"]))

    def stop(self) -> None:
        self._running = False
