"""Regression tests for the TMDB episode-404 storm (2026-10-23).

Production symptom: a series matched to a WRONG TMDB id made
``_adopt_meta`` walk every episode of every season — hundreds of 404s
per title — and because only HTTP 200 responses were cached, the whole
storm repeated on every enrich pass, saturating the event loop until
the bot stopped answering commands.

Fixes covered here:
- ``TMDB._get`` negative-caches non-200 responses (MISS_TTL), so a
  repeat lookup of a missing episode costs no HTTP call.
- ``_adopt_meta`` caps episode-title ATTEMPTS at 40 (not just
  successes) and bails after 6 consecutive misses.
- The happy path still fills episode titles.
"""
import tempfile
import unittest
from pathlib import Path

from app.classify import Classifier
from app.db import DB
from app.indexer import Indexer
from app.parser import parse_filename
from app.tmdb import TMDB


def _meta(chat, msg, name):
    return {"file_name": name, "size": 10, "duration": 5,
            "mime": "video/x-matroska", "tg_chat_id": chat,
            "tg_msg_id": msg, "caption": "", "msg_date": 0}


# -- fake aiohttp session ---------------------------------------------------

class _FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Minimal stand-in for the aiohttp session TMDB._sess returns."""

    closed = False

    def __init__(self, status, payload):
        self._status = status
        self._payload = payload
        self.calls = 0

    def get(self, url, params=None):
        self.calls += 1
        return _FakeCtx(_FakeResp(self._status, self._payload))


class NegativeCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = DB(Path(tempfile.mkdtemp()) / "tmdb404.db")

    async def test_404_is_negative_cached(self):
        sess = FakeSession(404, {"status_message": "not found"})
        tmdb = TMDB(self.db, "key", session=sess)
        self.assertEqual(await tmdb.episode_title(1, 1, 1), "")
        self.assertEqual(await tmdb.episode_title(1, 1, 1), "")
        self.assertEqual(sess.calls, 1)  # 2nd lookup served by miss cache

    async def test_200_is_returned_and_cached(self):
        sess = FakeSession(200, {"name": "Pilot"})
        tmdb = TMDB(self.db, "key", session=sess)
        self.assertEqual(await tmdb.episode_title(1, 1, 1), "Pilot")
        self.assertEqual(await tmdb.episode_title(1, 1, 1), "Pilot")
        self.assertEqual(sess.calls, 1)


# -- _adopt_meta storm guards -------------------------------------------------

class CountingTMDB:
    """episode_title stub: fn(call_no, season_no, ep_no) -> str."""

    def __init__(self, fn):
        self.fn = fn
        self.calls = 0

    async def episode_title(self, tmdb_id, season_no, ep_no):
        self.calls += 1
        return self.fn(self.calls, season_no, ep_no)


def _series_db(show, seasons_eps):
    """Ingest a series; seasons_eps = [(season_no, ep range), ...]."""
    db = DB(Path(tempfile.mkdtemp()) / "storm.db")
    clf = Classifier(db)
    msg = 1000
    for s_no, eps in seasons_eps:
        for e_no in eps:
            msg += 1
            clf.ingest(parse_filename(f"{show} S{s_no:02d}E{e_no:02d} 720p.mkv"),
                       _meta(-1, msg, "ep.mkv"))
    row = db.query_one("SELECT * FROM titles WHERE title=?", (show,))
    return db, row


def _full_meta(tmdb_id):
    return {"kind": "series", "tmdb_id": tmdb_id, "title": "Wrong Match Show",
            "orig_title": "", "alt_titles": [], "year": 2020,
            "poster": "", "backdrop": "", "overview": "", "genres": [],
            "rating": None, "popularity": None, "lang": "en"}


class AdoptStormTest(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_match_bails_after_six_consecutive_misses(self):
        db, row = _series_db("Wrong Match Show",
                             [(1, range(1, 13)), (2, range(1, 13)),
                              (3, range(1, 13))])
        idx = Indexer(None, db, TMDB(db, ""))
        stub = CountingTMDB(lambda n, s, e: "")
        idx.tmdb = stub
        await idx._adopt_meta(row, _full_meta(999001))
        self.assertLessEqual(stub.calls, 6)


class AttemptsCapTest(unittest.IsolatedAsyncioTestCase):
    async def test_attempts_capped_at_40(self):
        db, row = _series_db("Wrong Match Show",
                             [(s, range(1, 13)) for s in range(1, 6)])  # 60 eps
        idx = Indexer(None, db, TMDB(db, ""))
        # a hit every 5th call keeps consecutive misses at 4, so only the
        # attempts cap can stop this fill
        stub = CountingTMDB(lambda n, s, e: f"Ep {e}" if n % 5 == 0 else "")
        idx.tmdb = stub
        await idx._adopt_meta(row, _full_meta(999002))
        self.assertEqual(stub.calls, 40)
        filled = db.query_one(
            "SELECT COUNT(*) c FROM episodes WHERE ep_title != ''")["c"]
        self.assertGreater(filled, 0)


class FillWorksTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_episode_titles_filled(self):
        db, row = _series_db("Wrong Match Show", [(1, range(1, 13))])
        idx = Indexer(None, db, TMDB(db, ""))
        stub = CountingTMDB(lambda n, s, e: f"Episode {e}")
        idx.tmdb = stub
        await idx._adopt_meta(row, _full_meta(999003))
        n_eps = db.query_one("SELECT COUNT(*) c FROM episodes")["c"]
        self.assertEqual(stub.calls, n_eps)
        filled = db.query_one(
            "SELECT COUNT(*) c FROM episodes WHERE ep_title != ''")["c"]
        self.assertEqual(filled, n_eps)


if __name__ == "__main__":
    unittest.main()
