"""Classification: files -> canonical titles -> seasons/episodes.

Rules:
- A movie's 480p/720p/1080p files all attach to ONE title row.
- A series' S01E01/E02 files attach to episode rows under one season row.
- Matching order: (norm_title, year) exact -> tmdb_id -> FTS fuzzy.
- Ambiguous or unmatched files keep title_id NULL so they stay searchable
  with a placeholder poster; an admin can merge/reassign later.
"""
from __future__ import annotations

import logging
import time

from .db import DB, _j, _uj
from .parser import ParsedName, normalize_title

log = logging.getLogger("mediabot.classify")


class Classifier:
    def __init__(self, db: DB):
        self.db = db

    # -- title lookup ------------------------------------------------------
    def find_title(self, p: ParsedName) -> dict | None:
        db = self.db
        if p.year:
            row = db.query_one(
                "SELECT * FROM titles WHERE norm_title=? AND year=? AND kind=?",
                (p.norm_title, p.year, p.kind),
            )
            if row:
                return dict(row)
        rows = db.query(
            "SELECT * FROM titles WHERE norm_title=? AND kind=?", (p.norm_title, p.kind)
        )
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1 and p.year:
            for r in rows:
                if r["year"] == p.year:
                    return dict(r)
        # FTS fallback for minor differences (e.g. word order). The phrase
        # query also matches titles that merely START with the parsed title
        # — e.g. a leftover junk title "... 700Mb" — which used to capture
        # clean files right back onto the junk title. Accept a hit only
        # when its norm_title is exactly the same words: never a superset.
        if not rows:
            want = sorted(p.norm_title.split())
            fts = db.query(
                "SELECT t.* FROM titles_fts f JOIN titles t ON t.id=f.rowid "
                "WHERE titles_fts MATCH ? LIMIT 5",
                (f'"{p.norm_title}"',),
            )
            for r in fts:
                if sorted((r["norm_title"] or "").split()) != want:
                    continue
                if r["kind"] == p.kind and (not p.year or r["year"] in (p.year, None)):
                    return dict(r)
        return None

    def create_title(self, p: ParsedName, meta: dict | None) -> dict:
        now = time.time()
        if meta:
            title, kind = meta["title"], meta["kind"]
            norm = normalize_title(title)
            row = {
                "kind": kind, "title": title, "norm_title": norm,
                "orig_title": meta.get("orig_title", ""), "alt_titles": _j(meta.get("alt_titles", [])),
                "year": meta.get("year") or p.year, "tmdb_id": meta.get("tmdb_id"),
                "poster": meta.get("poster", ""), "backdrop": meta.get("backdrop", ""),
                "overview": meta.get("overview", ""), "genres": _j(meta.get("genres", [])),
                "rating": meta.get("rating"), "popularity": meta.get("popularity"),
                "lang": meta.get("lang", ""),
            }
        else:
            row = {
                "kind": p.kind, "title": p.title, "norm_title": p.norm_title,
                "orig_title": "", "alt_titles": "[]", "year": p.year, "tmdb_id": None,
                "poster": "", "backdrop": "", "overview": "", "genres": "[]",
                "rating": None, "popularity": None, "lang": "",
            }
        cur = self.db.execute(
            "INSERT INTO titles(kind,title,norm_title,orig_title,alt_titles,year,tmdb_id,"
            "poster,backdrop,overview,genres,rating,popularity,lang,added_at,updated_at,last_file_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["kind"], row["title"], row["norm_title"], row["orig_title"], row["alt_titles"],
             row["year"], row["tmdb_id"], row["poster"], row["backdrop"], row["overview"],
             row["genres"], row["rating"], row["popularity"], row["lang"], now, now, now),
        )
        return self.db.query_one("SELECT * FROM titles WHERE id=?", (cur.lastrowid,))  # type: ignore[return-value]

    # -- series structure ----------------------------------------------------
    def get_or_create_season(self, title_id: int, season_no: int) -> dict:
        row = self.db.query_one(
            "SELECT * FROM seasons WHERE title_id=? AND season_no=?", (title_id, season_no)
        )
        if row:
            return dict(row)
        self.db.execute(
            "INSERT OR IGNORE INTO seasons(title_id, season_no) VALUES(?,?)", (title_id, season_no)
        )
        return dict(self.db.query_one(
            "SELECT * FROM seasons WHERE title_id=? AND season_no=?", (title_id, season_no)))

    def get_or_create_episode(self, title_id: int, season_id: int, ep_no: int,
                               ep_title: str = "") -> dict:
        row = self.db.query_one(
            "SELECT * FROM episodes WHERE season_id=? AND ep_no=?", (season_id, ep_no)
        )
        if row:
            return dict(row)
        self.db.execute(
            "INSERT OR IGNORE INTO episodes(title_id, season_id, ep_no, ep_title) VALUES(?,?,?,?)",
            (title_id, season_id, ep_no, ep_title),
        )
        return dict(self.db.query_one(
            "SELECT * FROM episodes WHERE season_id=? AND ep_no=?", (season_id, ep_no)))

    # -- main entry ------------------------------------------------------------
    def ingest(self, p: ParsedName, file_meta: dict, tmdb_meta: dict | None = None) -> dict:
        """Record one Telegram file. Returns {'title_id', 'episode_id', 'file_id'}.

        file_meta: file_name, size, duration, mime, tg_chat_id, tg_msg_id,
                   caption, msg_date
        Idempotent on (tg_chat_id, tg_msg_id).
        """
        db = self.db
        now = time.time()
        existing = db.query_one(
            "SELECT id, title_id, episode_id FROM files WHERE tg_chat_id=? AND tg_msg_id=?",
            (file_meta["tg_chat_id"], file_meta["tg_msg_id"]),
        )
        if existing:
            return {"title_id": existing["title_id"], "episode_id": existing["episode_id"],
                    "file_id": existing["id"], "duplicate": True}

        title = self.find_title(p)
        if title is None:
            title = self.create_title(p, tmdb_meta)
        title_id = title["id"]
        episode_id = None
        season_id = None
        pack_label = ""

        if p.is_pack:
            # season pack: attaches to the season, not to an episode
            season = self.get_or_create_season(title_id, p.season or 1)
            season_id = season["id"]
            pack_label = p.pack_label or "Complete"
        elif p.kind == "series" and p.episode:
            season = self.get_or_create_season(title_id, p.season or 1)
            ep = self.get_or_create_episode(title_id, season["id"], p.episode)
            episode_id = ep["id"]

        cur = db.execute(
            "INSERT OR IGNORE INTO files(title_id, episode_id, season_id, pack_label, is_cam, file_name, norm_name, resolution,"
            " quality, codec, size, duration, mime, tg_chat_id, tg_msg_id, caption,"
            " msg_date, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title_id, episode_id, season_id, pack_label, 1 if p.is_cam else 0, file_meta["file_name"], p.norm_title, p.resolution,
             p.quality, p.codec, file_meta.get("size", 0), file_meta.get("duration", 0),
             file_meta.get("mime", ""), file_meta["tg_chat_id"], file_meta["tg_msg_id"],
             file_meta.get("caption", ""), file_meta.get("msg_date", 0), now),
        )
        db.execute(
            "UPDATE titles SET last_file_at=?, updated_at=? WHERE id=?", (now, now, title_id)
        )
        return {"title_id": title_id, "episode_id": episode_id,
                "file_id": cur.lastrowid, "duplicate": False}
