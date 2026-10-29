"""Regression test (2026-10-29): /api/home and /api/recent were doing
full-catalog temp-b-tree sorts (38-66s per query) because their WHERE
clauses didn't imply the partial indexes' WHERE (tmdb_id IS NOT NULL).
That saturated the 2 vCPUs, starved the event loop (6-10s lag), dropped
Telegram sessions, and hung the API. These tests assert the endpoint
SQL can use an index instead of scanning the whole titles table."""
import sqlite3

import pytest

from app import api as api_mod


def _plan(db_path, sql, params=()):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        return [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql, params)]
    finally:
        con.close()


# The exact SQL shapes the endpoints run (minus the CLEAN_ONLY toggle,
# which is an EXISTS subquery and doesn't affect index choice).
QUERIES = {
    "home hero": (
        "SELECT * FROM titles WHERE kind=? AND tmdb_id IS NOT NULL"
        " AND backdrop != '' AND overview != '' AND poster != ''"
        " ORDER BY last_file_at DESC LIMIT 4",
        ("movie",),
    ),
    "home rail": (
        "SELECT * FROM titles WHERE kind=? AND tmdb_id IS NOT NULL"
        " AND poster != '' AND year=2026"
        " ORDER BY popularity DESC NULLS LAST LIMIT 18",
        ("movie",),
    ),
    "home trending": (
        "SELECT * FROM titles WHERE tmdb_id IS NOT NULL AND poster != ''"
        " ORDER BY popularity DESC NULLS LAST LIMIT 18",
        (),
    ),
    "recent": (
        "SELECT * FROM titles WHERE tmdb_id IS NOT NULL AND poster != ''"
        " ORDER BY last_file_at DESC LIMIT 24",
        (),
    ),
    "recent kind": (
        "SELECT * FROM titles WHERE tmdb_id IS NOT NULL AND poster != ''"
        " AND kind=? ORDER BY last_file_at DESC LIMIT 24",
        ("movie",),
    ),
    "browse recent": (
        "SELECT * FROM titles WHERE tmdb_id IS NOT NULL AND poster != ''"
        " AND kind=? ORDER BY last_file_at DESC LIMIT 30 OFFSET 0",
        ("movie",),
    ),
    "browse az": (
        "SELECT * FROM titles WHERE tmdb_id IS NOT NULL AND poster != ''"
        " AND kind=? ORDER BY title COLLATE NOCASE ASC LIMIT 30 OFFSET 0",
        ("series",),
    ),
    "browse rating": (
        "SELECT * FROM titles WHERE tmdb_id IS NOT NULL AND poster != ''"
        " AND kind=? ORDER BY rating DESC NULLS LAST, popularity DESC NULLS LAST"
        " LIMIT 30 OFFSET 0",
        ("movie",),
    ),
    "browse year": (
        "SELECT * FROM titles WHERE tmdb_id IS NOT NULL AND poster != ''"
        " AND kind=? ORDER BY year DESC NULLS LAST, title COLLATE NOCASE ASC"
        " LIMIT 30 OFFSET 0",
        ("movie",),
    ),
}


@pytest.mark.parametrize("label", list(QUERIES))
def test_catalog_queries_use_index(tmp_path, label):
    # Build a small DB with the real schema so EXPLAIN QUERY PLAN is valid.
    from app.db import DB
    db = DB(str(tmp_path / "t.db"))
    sql, params = QUERIES[label]
    plan = _plan(str(tmp_path / "t.db"), sql, params)
    text = " | ".join(plan)
    # No full table scan: every plan must touch titles via an index.
    assert "SCAN titles" not in text or "USING INDEX" in text, text
    assert "USING INDEX" in text, text
