"""FTS5 prefix-index migration (2026-10-29).

Every search runs prefix queries like "robin"*, but titles_fts was created
without the prefix= option, so each search scanned the whole 1M+ row index
(~16s for a real user query). The table now carries prefix='2 3 4' and
existing DBs are rebuilt once from the titles table.
"""
from app.db import DB, TITLES_FTS_DDL


def _titles(db):
    return [
        ("movie", "Robin Hood", "robin hood"),
        ("movie", "The Death of Robin Hood", "the death of robin hood"),
    ]


def test_fresh_db_has_prefix_index(tmp_path):
    db = DB(tmp_path / "fresh.db")
    row = db.query_one(
        "SELECT sql FROM sqlite_master WHERE name='titles_fts'")
    assert "prefix=" in row["sql"]


def test_existing_db_is_migrated_once(tmp_path):
    p = tmp_path / "old.db"
    db = DB(p)
    # simulate a pre-migration DB: same table, no prefix option
    db.execute("DROP TABLE titles_fts")
    db.execute(TITLES_FTS_DDL.replace(", prefix='2 3 4'", ""))
    for kind, title, norm in _titles(db):
        db.execute(
            "INSERT INTO titles(kind, title, norm_title, added_at, updated_at)"
            " VALUES(?, ?, ?, 1, 1)", (kind, title, norm))
    row = db.query_one(
        "SELECT sql FROM sqlite_master WHERE name='titles_fts'")
    assert "prefix=" not in row["sql"]

    # reopening triggers the one-time migration
    db2 = DB(p)
    row = db2.query_one(
        "SELECT sql FROM sqlite_master WHERE name='titles_fts'")
    assert "prefix=" in row["sql"]
    # data preserved
    n = db2.query_one("SELECT COUNT(*) c FROM titles_fts")["c"]
    assert n == 2
    # prefix queries now hit the index
    hits = db2.query("SELECT rowid FROM titles_fts WHERE titles_fts MATCH ?",
                     ('"rob"*',))
    assert len(hits) == 2
    # reopening again is a no-op
    db3 = DB(p)
    row = db3.query_one(
        "SELECT sql FROM sqlite_master WHERE name='titles_fts'")
    assert "prefix=" in row["sql"]
    assert db3.query_one("SELECT COUNT(*) c FROM titles_fts")["c"] == 2
