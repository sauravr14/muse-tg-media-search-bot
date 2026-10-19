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
from .llm import LLTitle
from .db import DB
from .parser import ParsedName, parse_filename
from .tmdb import TMDB

log = logging.getLogger("mediabot.indexer")

MEDIA_TYPES = ("video", "document")
BATCH = 100
# Network guard: a hung Telegram connection must never wedge the scan lock.
GET_TIMEOUT = 90
# A batch that times out this many times in a row aborts the scan; the
# supervisor retries it later instead of spinning forever.
MAX_BATCH_RETRIES = 5
# Supervisor cadence and stall threshold.
SUPERVISOR_INTERVAL = 300
STALL_AFTER = 1200


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
    def __init__(self, client, db: DB, tmdb: TMDB, llm: LLTitle | None = None):
        # Bot-token only. History is read with messages.GetMessages by explicit
        # ID ranges (like Levii/EvaMaria filter bots) because Telegram blocks
        # bots from messages.GetHistory. The bot must be admin in private
        # channels; public channels work from a forwarded post or t.me link.
        self.client = client
        self.db = db
        self.tmdb = tmdb
        self.llm = llm
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
            m = await asyncio.wait_for(self.client.get_messages(chat_id, 1),
                                       timeout=GET_TIMEOUT)
            _ = m.empty  # touch it
            return True, ""
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            return await self.access_test(chat_id)
        except asyncio.TimeoutError:
            return False, "Telegram timed out — try again in a bit."
        except Exception as e:
            name = type(e).__name__
            if name in ("ChannelInvalid", "ChannelPrivate", "UsernameInvalid",
                        "UsernameNotModified", "PeerIdInvalid"):
                return False, ("I can't open that channel — make me an admin there "
                               "first (required for private channels).")
            return False, f"{name}: {e}"

    async def _msg_exists(self, chat_id: int, mid: int) -> bool:
        try:
            m = await asyncio.wait_for(self.client.get_messages(chat_id, mid),
                                       timeout=GET_TIMEOUT)
        except Exception:
            return False
        return m is not None and not getattr(m, "empty", False)

    async def _batch(self, chat_id: int, ids: list[int]) -> list | None:
        try:
            return await asyncio.wait_for(self.client.get_messages(chat_id, ids),
                                          timeout=GET_TIMEOUT)
        except Exception:
            return None

    async def _exists_above(self, chat_id: int, mid: int) -> bool:
        """Neighbourhood check: did a doubling probe just land in a deletion
        gap while higher messages still exist?"""
        msgs = await self._batch(chat_id, list(range(max(1, mid - 50), mid + 51)))
        return any(m is not None and not getattr(m, "empty", False)
                   and getattr(m, "id", 0) > mid for m in msgs or [])

    async def probe_top_id(self, chat_id: int) -> int:
        """Find the highest existing message id in a channel.

        Used for fresh sources that were added via /addchannel without a
        forwarded post (so no top_id is known). Doubling search for an empty
        upper bound, binary search to narrow it, then an upward batch scan
        (tolerates deletion gaps) for the exact top. Returns 0 if the top
        can't be determined.
        """
        if not await self._msg_exists(chat_id, 1):
            return 0
        lo, hi = 1, 2
        while hi < 100_000_000:
            if await self._msg_exists(chat_id, hi):
                lo, hi = hi, hi * 2
            elif await self._exists_above(chat_id, hi):
                hi *= 2  # hi sits in a deletion gap; keep lo, widen upward
            else:
                break
        # binary search for the rightmost existing id in [lo, hi]
        # (approximate under deletion gaps — the upward scan corrects it)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if await self._msg_exists(chat_id, mid):
                lo = mid
            else:
                hi = mid - 1
        # upward batch scan from the candidate; two consecutive all-empty
        # batches mean we've passed the top
        top, empty_streak, cur = lo, 0, lo + 1  # lo is known to exist
        for _ in range(30):
            msgs = await self._batch(chat_id, list(range(cur, cur + 100)))
            if msgs is None:
                break
            real = [m.id for m in msgs
                    if m is not None and not getattr(m, "empty", False)]
            if real:
                top = max(top, max(real))
                empty_streak = 0
            else:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            cur += 100
        return top

    async def scan_source(self, chat_id: int, top_id: int = 0,
                        progress_cb=None) -> dict:
        """Scan message IDs (checkpoint+1 .. top_id) in batches via GetMessages.

        top_id comes from a forwarded post or t.me link. Falls back to the
        highest ID seen via live posts/forwards. Resumable and idempotent.

        progress_cb(done, total) is called after each completed batch so the
        caller can report progress on long scans.
        """
        async with self._scan_lock:
            src = self.db.query_one("SELECT * FROM sources WHERE chat_id=?", (chat_id,))
            if not src:
                return {"error": "unknown source"}
            if top_id:
                self.note_seen(chat_id, top_id)
            hi = top_id or self.max_seen(chat_id)
            lo = (src["last_msg_id"] or 0) + 1
            # /setskip: ignore the first N messages of a channel on fresh scans
            skip = src["skip_first"] or 0
            if skip:
                lo = max(lo, skip + 1)
            if hi < lo:
                # Fresh source with no known top (added via /addchannel, never
                # forwarded): probe the channel's top message id so the scan
                # can start without needing a forwarded post.
                if not top_id and not (src["last_msg_id"] or 0) \
                        and not self.max_seen(chat_id):
                    probed = await self.probe_top_id(chat_id)
                    if probed:
                        self.note_seen(chat_id, probed)
                        hi = probed
                if hi < lo:
                    # Up to date as far as we know — clear any stale error so the
                    # supervisor and /channels stop reporting a dead failure.
                    if (src["status"] or "") == "error":
                        self.db.set_source(chat_id, status="idle", error="")
                    return {"scanned": 0, "new": 0, "note": "nothing new to scan"}
            self.db.set_source(chat_id, status="scanning", error="")
            self._heartbeat(chat_id)
            ok, reason = await self.access_test(chat_id)
            if not ok:
                self.db.set_source(chat_id, status="error", error=reason)
                return {"scanned": 0, "new": 0, "error": reason}
            done = new = 0
            cur = lo
            try:
                while cur <= hi:
                    batch_end = min(cur + BATCH - 1, hi)
                    ids = list(range(cur, batch_end + 1))
                    msgs = None
                    for attempt in range(MAX_BATCH_RETRIES):
                        try:
                            msgs = await asyncio.wait_for(
                                self.client.get_messages(chat_id, ids),
                                timeout=GET_TIMEOUT)
                            break
                        except FloodWait as e:
                            await asyncio.sleep(e.value + 1)
                        except (asyncio.TimeoutError, OSError) as e:
                            # hung/flaky connection: back off and retry the
                            # same batch instead of dying or wedging the lock
                            log.warning("scan of %s: batch %s-%s %s (try %d/%d)",
                                        chat_id, cur, batch_end,
                                        type(e).__name__, attempt + 1,
                                        MAX_BATCH_RETRIES)
                            await asyncio.sleep(5 * (attempt + 1))
                    if msgs is None:
                        raise RuntimeError(
                            f"batch {cur}-{batch_end} failed {MAX_BATCH_RETRIES}x; "
                            "will resume from checkpoint")
                    # one DB transaction per batch: ~7 commits/file -> 1 commit/batch
                    with self.db.transaction():
                        for i, msg in enumerate(msgs):
                            done += 1
                            if msg is None or getattr(msg, "empty", False):
                                continue
                            r = await self.ingest_message(msg)
                            if r and not r.get("duplicate"):
                                new += 1
                            # let the bot/API breathe between files
                            if i % 25 == 24:
                                await asyncio.sleep(0)
                        # checkpoint = last message id of the completed batch
                        self.db.set_source(chat_id, last_msg_id=batch_end)
                    self._heartbeat(chat_id)
                    if progress_cb:
                        progress_cb(done, hi - lo + 1)
                    cur = batch_end + 1
                    await asyncio.sleep(0.4)
            except Exception as e:
                log.warning("scan of %s failed: %r", chat_id, e)
                self.db.set_source(chat_id, status="error",
                                   error=f"{type(e).__name__}: {e}")
                return {"scanned": done, "new": new, "error": str(e)}
            self.db.set_source(chat_id, status="idle", last_msg_id=hi)
            return {"scanned": done, "new": new}

    def _heartbeat(self, chat_id: int) -> None:
        self.db.set_kv(f"scan_hb:{chat_id}", str(time.time()))

    def _last_heartbeat(self, chat_id: int) -> float:
        v = self.db.get_kv(f"scan_hb:{chat_id}")
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    async def scan_supervisor(self) -> None:
        """Background loop: retry scans that died or stalled.

        A scan that ends in status='error' (e.g. 'Connection lost' on a flaky
        network) is resumed from its checkpoint. A scan stuck in
        status='scanning' with no heartbeat for STALL_AFTER seconds is treated
        as dead and retried too. Runs until stop() is called.
        """
        self._running = True
        log.info("scan supervisor started (every %ds)", SUPERVISOR_INTERVAL)
        while self._running:
            try:
                await self._supervisor_tick()
            except Exception as e:
                log.warning("supervisor tick failed: %r", e)
            for _ in range(SUPERVISOR_INTERVAL):
                if not self._running:
                    break
                await asyncio.sleep(1)

    async def _supervisor_tick(self) -> None:
        """One pass over sources: resume errored scans, reboot stalled ones."""
        for src in self.db.list_sources():
            if not self._running:
                break
            if not src["enabled"]:
                continue
            status = src["status"] or ""
            if status == "error":
                log.info("supervisor: resuming errored scan of %s (%s)",
                         src["chat_id"], (src["error"] or "")[:80])
                await self.scan_source(src["chat_id"])
            elif status == "scanning":
                idle_for = time.time() - self._last_heartbeat(src["chat_id"])
                if idle_for > STALL_AFTER:
                    log.warning("supervisor: scan of %s stalled %.0fs, retrying",
                                src["chat_id"], idle_for)
                    self.db.set_source(src["chat_id"], status="error",
                                       error=f"stalled: no progress for {int(idle_for)}s")
                    await self.scan_source(src["chat_id"])

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
                with self.db.transaction():
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
        from .parser import normalize_title
        row = self.db.query_one("SELECT * FROM titles WHERE id=?", (title_id,))
        if not row or row["tmdb_id"]:
            return
        self.db.set_kv(f"tmdb_try:{title_id}", str(time.time()))
        p = ParsedName(raw=row["title"], title=row["title"],
                       norm_title=row["norm_title"], year=row["year"], kind=row["kind"])
        meta = await self.tmdb.match(p)
        if not meta and self.llm and self.llm.available:
            meta = await self._enrich_via_llm(row, p)
        if not meta:
            return
        # adopt canonical TMDB title; merge possible duplicate title rows
        await self._adopt_meta(row, meta)

    async def _enrich_via_llm(self, row, p) -> dict | None:
        """Ask the LLM to identify the title from a raw release filename,
        then cross-check its guess against TMDB (threshold still applies)."""
        from .parser import normalize_title
        fn = self.db.query_one("SELECT file_name FROM files WHERE title_id=? LIMIT 1",
                               (row["id"],))
        filename = fn["file_name"] if fn else p.raw
        ck = self.llm.cache_key(filename)
        cached = self.db.get_kv(ck)
        guess = None
        if cached:
            try:
                guess = __import__("json").loads(cached)
            except Exception:
                guess = None
        if guess is None:
            guess = await self.llm.guess_title(filename)
            self.db.set_kv(ck, __import__("json").dumps(guess or {}))
            await asyncio.sleep(0.5)
        if not guess or not guess.get("title"):
            return None
        tried = set()
        for variant in (guess["title"], guess["title"].split(":")[0],
                        guess["title"].split(" - ")[0]):
            variant = variant.strip()
            if not variant or variant.lower() in tried:
                continue
            tried.add(variant.lower())
            p2 = ParsedName(raw=p.raw, title=variant,
                            norm_title=normalize_title(variant),
                            year=guess.get("year") or p.year,
                            kind=guess.get("kind") or p.kind)
            meta = await self.tmdb.match(p2)
            if meta:
                log.info("llm identified %r as %r (%s)", p.raw[:60],
                         meta["title"], meta.get("year"))
                return meta
        return None

    async def _merge_titles(self, old_id: int, new_id: int) -> None:
        """Fold duplicate title old_id into new_id (same TMDB id).

        Moves seasons/episodes/files over, merging same-numbered seasons and
        episodes, so no file loses its episode association. (A naive
        DELETE FROM seasons would cascade and null out files.episode_id.)"""
        db = self.db
        for se in db.query("SELECT * FROM seasons WHERE title_id=?", (old_id,)):
            tgt = db.query_one(
                "SELECT id FROM seasons WHERE title_id=? AND season_no=?",
                (new_id, se["season_no"]))
            if tgt:
                tgt_id = tgt["id"]
                for ep in db.query("SELECT * FROM episodes WHERE season_id=?",
                                   (se["id"],)):
                    ex = db.query_one(
                        "SELECT id FROM episodes WHERE season_id=? AND ep_no=?",
                        (tgt_id, ep["ep_no"]))
                    if ex:
                        db.execute("UPDATE files SET episode_id=?, season_id=? "
                                   "WHERE episode_id=?", (ex["id"], tgt_id, ep["id"]))
                        db.execute("DELETE FROM episodes WHERE id=?", (ep["id"],))
                    else:
                        db.execute("UPDATE episodes SET season_id=?, title_id=? "
                                   "WHERE id=?", (tgt_id, new_id, ep["id"]))
                db.execute("UPDATE files SET season_id=? WHERE season_id=?",
                           (tgt_id, se["id"]))
                db.execute("DELETE FROM seasons WHERE id=?", (se["id"],))
            else:
                db.execute("UPDATE seasons SET title_id=? WHERE id=?",
                           (new_id, se["id"]))
                db.execute("UPDATE episodes SET title_id=? WHERE season_id=?",
                           (new_id, se["id"]))
                # pack files keep their season_id: the season row itself moved
        db.execute("UPDATE files SET title_id=? WHERE title_id=?", (new_id, old_id))
        db.execute("DELETE FROM titles WHERE id=?", (old_id,))
        db.execute("UPDATE titles SET last_file_at=? WHERE id=?",
                   (time.time(), new_id))
        log.info("merged title %s into %s", old_id, new_id)

    async def _adopt_meta(self, row, meta: dict) -> None:
        from .parser import normalize_title
        db = self.db
        now = time.time()
        # if another title already owns this tmdb_id, merge into it
        other = db.query_one("SELECT id FROM titles WHERE tmdb_id=? AND id != ?",
                             (meta["tmdb_id"], row["id"]))
        if other:
            await self._merge_titles(row["id"], other["id"])
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
