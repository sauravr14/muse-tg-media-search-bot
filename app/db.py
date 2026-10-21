"""SQLite catalog: canonical titles, seasons, episodes, file records, sources.

Single source of truth. WAL mode. FTS5 for title search. Every write goes
through this module; all queries are parameterized.
"""
from __future__ import annotations

import contextlib
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
    skip_first  INTEGER NOT NULL DEFAULT 0,
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
    status      TEXT NOT NULL DEFAULT 'ok',
    has_clean   INTEGER NOT NULL DEFAULT 1,
    tmdb_tries  INTEGER NOT NULL DEFAULT 0,
    tmdb_next_at REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_titles_tmdb ON titles(tmdb_id) WHERE tmdb_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_titles_norm ON titles(norm_title, year);
CREATE INDEX IF NOT EXISTS idx_titles_kind ON titles(kind);
CREATE INDEX IF NOT EXISTS idx_titles_recent ON titles(last_file_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS titles_fts USING fts5(
    title, orig_title, alt_titles, norm_title,
    content='titles', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2',
    prefix='2 3 4'
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
    season_id   INTEGER REFERENCES seasons(id) ON DELETE SET NULL,
    pack_label  TEXT NOT NULL DEFAULT '',
    is_cam      INTEGER NOT NULL DEFAULT 0,
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
    tg_file_id  TEXT NOT NULL DEFAULT '',
    UNIQUE(tg_chat_id, tg_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_files_title ON files(title_id);
CREATE INDEX IF NOT EXISTS idx_files_title_ep ON files(title_id, episode_id);
CREATE INDEX IF NOT EXISTS idx_files_title_season ON files(title_id, season_id);
CREATE INDEX IF NOT EXISTS idx_files_episode ON files(episode_id);
CREATE INDEX IF NOT EXISTS idx_files_recent ON files(indexed_at DESC);
-- FK-cascade indexes: deleting a title cascades into seasons/episodes.
-- Without these, each deleted title triggers a full table scan of the
-- child tables; the /cleanup orphan phase once starved the event loop
-- for ~1h on a 542k-file catalog (2026-10-21). Created on next startup
-- via CREATE INDEX IF NOT EXISTS.
CREATE INDEX IF NOT EXISTS idx_seasons_title ON seasons(title_id);
CREATE INDEX IF NOT EXISTS idx_episodes_title ON episodes(title_id);
CREATE INDEX IF NOT EXISTS idx_episodes_season ON episodes(season_id);

CREATE TABLE IF NOT EXISTS tmdb_cache (
    key         TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- messages the bot must delete after the self-destruct timer expires
-- (survives restarts: pending rows are re-scheduled on boot)
CREATE TABLE IF NOT EXISTS pending_deletes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    msg_id    INTEGER NOT NULL,
    delete_at REAL NOT NULL,
    UNIQUE(chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_pending_deletes_due ON pending_deletes(delete_at);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()  # reentrant: transaction() + execute() nest
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            # big cache for the main (bot/indexer) thread, small for API
            # worker threads so many threads can't exhaust a 1 GB VPS
            main = threading.current_thread() is threading.main_thread()
            conn.execute(f"PRAGMA cache_size=-{65536 if main else 8192}")
            conn.execute("PRAGMA mmap_size=268435456")    # 256 MB memory-mapped reads
            self._local.conn = conn
        return conn

    def _init(self) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.executescript(SCHEMA)
            # lightweight migrations for existing DBs
            src_cols = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
            if "skip_first" not in src_cols:
                conn.execute("ALTER TABLE sources ADD COLUMN skip_first INTEGER "
                             "NOT NULL DEFAULT 0")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
            if "season_id" not in cols:
                conn.execute("ALTER TABLE files ADD COLUMN season_id INTEGER "
                             "REFERENCES seasons(id) ON DELETE SET NULL")
            if "pack_label" not in cols:
                conn.execute("ALTER TABLE files ADD COLUMN pack_label TEXT "
                             "NOT NULL DEFAULT ''")
            if "is_cam" not in cols:
                conn.execute("ALTER TABLE files ADD COLUMN is_cam INTEGER "
                             "NOT NULL DEFAULT 0")
            conn.commit()
            self._migrate_v2(conn)

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        """Performance/reliability migration, idempotent (guarded per step).

        - titles.has_clean / tmdb_tries / tmdb_next_at, files.tg_file_id
        - partial indexes so Home/Browse/Search never scan the whole catalog
        - FTS5 rebuilt with prefix indexes (fast type-ahead search)
        - re-queue titles whose enrichment was wrongly parked for 7 days
          (TMDB network errors used to count as 'tried') and clear LLM
          cache entries that were poisoned by rate-limit failures.
        """
        tcols = {r[1] for r in conn.execute("PRAGMA table_info(titles)")}
        if "has_clean" not in tcols:
            conn.execute("ALTER TABLE titles ADD COLUMN has_clean INTEGER NOT NULL DEFAULT 1")
            conn.execute(
                "UPDATE titles SET has_clean = EXISTS("
                "SELECT 1 FROM files f WHERE f.title_id=titles.id AND f.is_cam=0)")
        if "tmdb_tries" not in tcols:
            conn.execute("ALTER TABLE titles ADD COLUMN tmdb_tries INTEGER NOT NULL DEFAULT 0")
        if "tmdb_next_at" not in tcols:
            conn.execute("ALTER TABLE titles ADD COLUMN tmdb_next_at REAL NOT NULL DEFAULT 0")
        fcols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
        if "tg_file_id" not in fcols:
            conn.execute("ALTER TABLE files ADD COLUMN tg_file_id TEXT NOT NULL DEFAULT ''")
        conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_titles_home ON titles(kind, last_file_at DESC)
            WHERE tmdb_id IS NOT NULL AND poster != '';
        CREATE INDEX IF NOT EXISTS idx_titles_pop ON titles(popularity DESC)
            WHERE tmdb_id IS NOT NULL AND poster != '';
        CREATE INDEX IF NOT EXISTS idx_titles_rating ON titles(kind, rating DESC)
            WHERE tmdb_id IS NOT NULL AND poster != '';
        CREATE INDEX IF NOT EXISTS idx_titles_year ON titles(kind, year DESC)
            WHERE tmdb_id IS NOT NULL AND poster != '';
        CREATE INDEX IF NOT EXISTS idx_titles_az ON titles(kind, title COLLATE NOCASE)
            WHERE tmdb_id IS NOT NULL AND poster != '';
        CREATE INDEX IF NOT EXISTS idx_titles_unmatched ON titles(tmdb_next_at)
            WHERE tmdb_id IS NULL;
        CREATE INDEX IF NOT EXISTS idx_files_title_clean ON files(title_id, is_cam, status);
        """)
        conn.commit()
        if self._kv_conn(conn, "migr_fts_prefix") != "1":
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='titles_fts'").fetchone()
            if sql and "prefix" not in (sql[0] or ""):
                conn.executescript("""
                DROP TRIGGER IF EXISTS trg_titles_ai;
                DROP TRIGGER IF EXISTS trg_titles_ad;
                DROP TRIGGER IF EXISTS trg_titles_au;
                DROP TABLE IF EXISTS titles_fts;
                """)
                conn.executescript(SCHEMA)  # recreates FTS (+prefix) and triggers
                conn.execute("INSERT INTO titles_fts(titles_fts) VALUES('rebuild')")
            self._kv_set_conn(conn, "migr_fts_prefix", "1")
        if self._kv_conn(conn, "migr_requeue_v2") != "1":
            conn.execute("UPDATE titles SET tmdb_next_at=0, tmdb_tries=0 WHERE tmdb_id IS NULL")
            conn.execute("DELETE FROM kv WHERE key LIKE 'tmdb_try:%'")
            conn.execute("DELETE FROM kv WHERE key LIKE 'llm_guess:%' AND value='{}'")
            self._kv_set_conn(conn, "migr_requeue_v2", "1")
        conn.commit()

    @staticmethod
    def _kv_conn(conn: sqlite3.Connection, key: str) -> str:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    @staticmethod
    def _kv_set_conn(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT INTO kv(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # -- generic helpers -------------------------------------------------
    def _in_tx(self) -> bool:
        return bool(getattr(self._local, "in_tx", False))

    @contextlib.contextmanager
    def transaction(self):
        """Batch many writes into a single commit.

        During big scans each file otherwise costs ~7 separate commits;
        wrapping a batch in one transaction is the single biggest indexing
        speedup. Nested use is safe (inner blocks join the outer one).
        """
        if self._in_tx():
            yield self._conn()
            return
        with self._write_lock:
            self._local.in_tx = True
            try:
                yield self._conn()
                self._conn().commit()
            except Exception:
                self._conn().rollback()
                raise
            finally:
                self._local.in_tx = False

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._write_lock:
            cur = self._conn().execute(sql, params)
            if not self._in_tx():
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

    # -- self-destructing deliveries --------------------------------------
    def add_pending_delete(self, chat_id: int, msg_id: int, delete_at: float) -> None:
        self.execute(
            "INSERT OR IGNORE INTO pending_deletes(chat_id, msg_id, delete_at)"
            " VALUES(?,?,?)", (chat_id, msg_id, delete_at))

    def remove_pending_delete(self, chat_id: int, msg_id: int) -> None:
        self.execute("DELETE FROM pending_deletes WHERE chat_id=? AND msg_id=?",
                     (chat_id, msg_id))

    def list_pending_deletes(self) -> list[sqlite3.Row]:
        return self.query("SELECT chat_id, msg_id, delete_at FROM pending_deletes"
                          " ORDER BY delete_at")

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
