"""SQLite catalog: canonical titles, seasons, episodes, file records, sources.

Single source of truth. WAL mode. FTS5 for title search. Every write goes
through this module; all queries are parameterized.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL UNIQUE,
    title       TEXT NOT NULL DEFAULT '',
    username    TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_msg_id INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'idle',
    error       TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS titles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK(kind IN ('movie','series')),
    title       TEXT NOT NULL,
    norm_title  TEXT NOT NULL,
    orig_title  TEXT NOT NULL DEFAULT '',
    alt_titles  TEXT NOT NULL DEFAULT '[]',
    year        INTEGER,
    tmdb_id     INTEGER,
    poster      TEXT NOT NULL DEFAULT '',
    backdrop    TEXT NOT NULL DEFAULT '',
    overview    TEXT NOT NULL DEFAULT '',
    genres      TEXT NOT NULL DEFAULT '[]',
    rating      REAL,
    popularity  REAL,
    lang        TEXT NOT NULL DEFAULT '',
    added_at    REAL NOT NULL,
    updated_at  REAL NOT NULL,
    last_file_at REAL NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'ok'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_titles_tmdb ON titles(tmdb_id) WHERE tmdb_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_titles_norm ON titles(norm_title, year);
CREATE INDEX IF NOT EXISTS idx_titles_kind ON titles(kind);
CREATE INDEX IF NOT EXISTS idx_titles_recent ON titles(last_file_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS titles_fts USING fts5(
    title, orig_title, alt_titles, norm_title,
    content='titles', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS trg_titles_ai AFTER INSERT ON titles BEGIN
    INSERT INTO titles_fts(rowid, title, orig_title, alt_titles, norm_title)
    VALUES (new.id, new.title, new.orig_title, new.alt_titles, new.norm_title);
END;
CREATE TRIGGER IF NOT EXISTS trg_titles_ad AFTER DELETE ON titles BEGIN
    INSERT INTO titles_fts(titles_fts, rowid, title, orig_title, alt_titles, norm_title)
    VALUES ('delete', old.id, old.title, old.orig_title, old.alt_titles, old.norm_title);
END;
CREATE TRIGGER IF NOT EXISTS trg_titles_au AFTER UPDATE ON titles BEGIN
    INSERT INTO titles_fts(titles_fts, rowid, title, orig_title, alt_titles, norm_title)
    VALUES ('delete', old.id, old.title, old.orig_title, old.alt_titles, old.norm_title);
    INSERT INTO titles_fts(rowid, title, orig_title, alt_titles, norm_title)
    VALUES (new.id, new.title, new.orig_title, new.alt_titles, new.norm_title);
END;

CREATE TABLE IF NOT EXISTS seasons (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id      INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    season_no     INTEGER NOT NULL,
    tmdb_season_id INTEGER,
    poster        TEXT NOT NULL DEFAULT '',
    overview      TEXT NOT NULL DEFAULT '',
    UNIQUE(title_id, season_no)
);

CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id    INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    season_id   INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    ep_no       INTEGER NOT NULL,
    ep_title    TEXT NOT NULL DEFAULT '',
    tmdb_ep_id  INTEGER,
    air_date    TEXT NOT NULL DEFAULT '',
    overview    TEXT NOT NULL DEFAULT '',
    UNIQUE(season_id, ep_no)
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id    INTEGER REFERENCES titles(id) ON DELETE SET NULL,
    episode_id  INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
    file_name   TEXT NOT NULL,
    norm_name   TEXT NOT NULL DEFAULT '',
    resolution  TEXT NOT NULL DEFAULT '',
    quality     TEXT NOT NULL DEFAULT '',
    codec       TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    duration    INTEGER NOT NULL DEFAULT 0,
    mime        TEXT NOT NULL DEFAULT '',
    tg_chat_id  INTEGER NOT NULL,
    tg_msg_id   INTEGER NOT NULL,
    caption     TEXT NOT NULL DEFAULT '',
    msg_date    REAL NOT NULL DEFAULT 0,
    indexed_at  REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ok',
    UNIQUE(tg_chat_id, tg_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_files_title ON files(title_id);
CREATE INDEX IF NOT EXISTS idx_files_episode ON files(episode_id);
CREATE INDEX IF NOT EXISTS idx_files_recent ON files(indexed_at DESC);

CREATE TABLE IF NOT EXISTS tmdb_cache (
    key         TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _init(self) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.executescript(SCHEMA)
            conn.commit()

    # -- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._write_lock:
            cur = self._conn().execute(sql, params)
            self._conn().commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, params).fetchone()

    def get_kv(self, key: str, default: str = "") -> str:
        row = self.query_one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- sources ----------------------------------------------------------
    def upsert_source(self, chat_id: int, title: str = "", username: str = "") -> int:
        now = time.time()
        self.execute(
            "INSERT INTO sources(chat_id, title, username, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, "
            "username=excluded.username, updated_at=excluded.updated_at",
            (chat_id, title, username, now),
        )
        row = self.query_one("SELECT id FROM sources WHERE chat_id=?", (chat_id,))
        return int(row["id"])

    def list_sources(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM sources ORDER BY id")

    def set_source(self, chat_id: int, **fields) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE sources SET {cols} WHERE chat_id=?", (*fields.values(), chat_id))

    def remove_source(self, chat_id: int) -> None:
        self.execute("DELETE FROM sources WHERE chat_id=?", (chat_id,))

    # -- stats ------------------------------------------------------------
    def stats(self) -> dict:
        out = {}
        for key, sql in {
            "files": "SELECT COUNT(*) c FROM files",
            "movies": "SELECT COUNT(*) c FROM titles WHERE kind='movie'",
            "series": "SELECT COUNT(*) c FROM titles WHERE kind='series'",
            "episodes": "SELECT COUNT(*) c FROM episodes",
            "sources": "SELECT COUNT(*) c FROM sources WHERE enabled=1",
            "unmatched": "SELECT COUNT(*) c FROM files WHERE title_id IS NULL",
        }.items():
            row = self.query_one(sql)
            out[key] = int(row["c"]) if row else 0
        return out


def _j(val) -> str:
    return json.dumps(val, ensure_ascii=False)


# Numeric ordering for resolution labels (string sort would put 480p > 1080p).
RES_ORDER = (
    "CASE resolution WHEN '2160p' THEN 4 WHEN '1080p' THEN 3 "
    "WHEN '720p' THEN 2 WHEN '480p' THEN 1 WHEN '360p' THEN 0 ELSE -1 END"
)


def _uj(raw: str, default=None):
    try:
        return json.loads(raw) if raw else default
    except (ValueError, TypeError):
        return default
