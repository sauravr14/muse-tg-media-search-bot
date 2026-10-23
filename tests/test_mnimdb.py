"""Tests for the MN IMDb poster fallback (mocked HTTP).

Context: TMDB leaves a slice of the catalog unmatched (no API result or
below the confidence floor) and those titles show placeholder posters
in the Mini App forever. The MN IMDb API is a free OMDb-shaped search;
it fills posters ONLY — never tmdb_id, never a merge — under strict
matching (exact normalized title, kind-compatible Type, year ±1),
because a wrong poster is worse than a missing one.
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")

import app.mnimdb as mnimdb_mod
from app.db import DB
from app.indexer import Indexer
from app.mnimdb import MNImdb


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class FakeSession:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status
        self.gets = []

    def get(self, url, params=None, timeout=None):
        self.gets.append((url, params))
        return FakeResp(self._status, self._payload)

    @property
    def closed(self):
        return False


class BoomSession:
    def __init__(self):
        self.gets = []

    def get(self, url, params=None, timeout=None):
        self.gets.append((url, params))
        raise ConnectionError("boom")

    @property
    def closed(self):
        return False


POSTER_TV = "https://m.media-amazon.com/images/M/death-note-tv.jpg"
POSTER_FILM = "https://m.media-amazon.com/images/M/death-note-film.jpg"
DEATH_NOTE_SEARCH = {
    "Search": [
        {"Title": "Death Note", "Year": "2006", "imdbID": "tt0877057",
         "Type": "TV series", "Poster": POSTER_TV},
        {"Title": "Death Note", "Year": "2017", "imdbID": "tt1241317",
         "Type": "feature", "Poster": POSTER_FILM},
        {"Title": "Death Note", "Year": "2015", "imdbID": "tt4623604",
         "Type": "TV mini-series", "Poster": "https://m.media-amazon.com/x.jpg"},
    ],
    "totalResults": "8", "Response": "True", "page": 1, "nextPage": None,
}


def mn_with(payload, status=200):
    return MNImdb("https://example.test", session=FakeSession(payload, status))


class TestSearch(unittest.TestCase):
    def test_search_returns_candidates(self):
        mn = mn_with(DEATH_NOTE_SEARCH)
        results = asyncio.run(mn.search("Death Note"))
        self.assertEqual(len(results), 3)
        url, params = mn._session.gets[0]
        self.assertTrue(url.endswith("/api/search"))
        self.assertEqual(params, {"q": "Death Note"})

    def test_no_results_is_empty_list_not_failure(self):
        mn = mn_with({"Response": "False", "Error": "Movie not found!"})
        self.assertEqual(asyncio.run(mn.search("asdfqwer")), [])

    def test_empty_base_url_not_available(self):
        mn = MNImdb("", session=FakeSession(DEATH_NOTE_SEARCH))
        self.assertFalse(mn.available)
        self.assertIsNone(asyncio.run(mn.search("Death Note")))
        self.assertEqual(mn._session.gets, [])

    def test_http_error_returns_none_breaker_stays_closed(self):
        mn = mn_with({}, status=500)
        self.assertIsNone(asyncio.run(mn.search("Death Note")))
        self.assertEqual(mn.unavailable_until, 0.0)

    def test_exception_returns_none(self):
        mn = MNImdb("https://example.test", session=BoomSession())
        self.assertIsNone(asyncio.run(mn.search("Death Note")))

    def test_429_trips_breaker_and_fails_fast(self):
        mn = mn_with({}, status=429)
        self.assertIsNone(asyncio.run(mn.search("Death Note")))
        self.assertGreater(mn.unavailable_until, time.time())
        # during cooldown: no HTTP at all, even with a healthy session
        mn._session = FakeSession(DEATH_NOTE_SEARCH)
        self.assertIsNone(asyncio.run(mn.search("Death Note")))
        self.assertEqual(mn._session.gets, [])

    def test_repeated_5xx_trips_breaker(self):
        mn = mn_with({}, status=503)

        async def main():
            for _ in range(3):
                self.assertIsNone(await mn.search("Death Note"))
            self.assertGreater(mn.unavailable_until, time.time())
            self.assertIsNone(await mn.search("Death Note"))

        with mock.patch.object(mnimdb_mod, "MIN_INTERVAL", 0):
            asyncio.run(main())
        self.assertEqual(len(mn._session.gets), 3)  # 4th call never left

    def test_calls_are_paced(self):
        sleeps = []

        async def rec_sleep(d):
            sleeps.append(d)

        mn = mn_with(DEATH_NOTE_SEARCH)

        async def main():
            await mn.search("Death Note")
            await mn.search("Death Note")

        with mock.patch.object(asyncio, "sleep", rec_sleep):
            asyncio.run(main())
        self.assertTrue(any(d > 0 for d in sleeps))


class TestPick(unittest.TestCase):
    def pick(self, cands, want="death note", year=2006, kind="series"):
        return MNImdb.pick(cands, want, year, kind)

    def test_series_picks_series_not_feature(self):
        self.assertEqual(self.pick(DEATH_NOTE_SEARCH["Search"]), POSTER_TV)

    def test_movie_picks_feature(self):
        self.assertEqual(
            self.pick(DEATH_NOTE_SEARCH["Search"], year=2017, kind="movie"),
            POSTER_FILM)

    def test_year_off_by_two_rejected(self):
        cands = [{"Title": "Death Note", "Year": "2008",
                  "imdbID": "tt0877057", "Type": "TV series",
                  "Poster": POSTER_TV}]
        self.assertIsNone(self.pick(cands))

    def test_year_off_by_one_accepted(self):
        cands = [{"Title": "Death Note", "Year": "2007",
                  "imdbID": "tt0877057", "Type": "TV series",
                  "Poster": POSTER_TV}]
        self.assertEqual(self.pick(cands), POSTER_TV)

    def test_series_year_range_parsed(self):
        cands = [{"Title": "Death Note", "Year": "2006\u20132013",
                  "imdbID": "tt0877057", "Type": "TV series",
                  "Poster": POSTER_TV}]
        self.assertEqual(self.pick(cands), POSTER_TV)

    def test_na_poster_skipped_for_later_valid(self):
        cands = [
            {"Title": "Death Note", "Year": "2006", "imdbID": "tt0877057",
             "Type": "TV series", "Poster": "N/A"},
            {"Title": "Death Note", "Year": "2006", "imdbID": "tt0877058",
             "Type": "TV series", "Poster": POSTER_TV},
        ]
        self.assertEqual(self.pick(cands), POSTER_TV)
        self.assertIsNone(self.pick(cands[:1]))

    def test_junk_types_and_person_entries_filtered(self):
        cands = [
            {"Title": "Death Note", "Year": "2006", "imdbID": "nm1234567",
             "Type": "TV series", "Poster": POSTER_TV},
            {"Title": "Death Note", "Year": "2006", "imdbID": "tt9999999",
             "Type": "musicVideo", "Poster": POSTER_TV},
        ]
        self.assertIsNone(self.pick(cands))

    def test_title_mismatch_rejected(self):
        cands = [{"Title": "Death Note Relight", "Year": "2007",
                  "imdbID": "tt3355694", "Type": "TV movie",
                  "Poster": POSTER_TV}]
        self.assertIsNone(self.pick(cands, kind="movie"))

    def test_unknown_catalog_year_accepts(self):
        self.assertEqual(
            self.pick(DEATH_NOTE_SEARCH["Search"], year=None), POSTER_TV)


class FakeTMDBNone:
    async def match(self, p):
        return None


class FakeTMDBHit:
    async def match(self, p):
        return {"tmdb_id": 42, "kind": "movie", "title": "A Heart in Winter",
                "year": 1992, "poster": "tmdb-poster", "backdrop": "",
                "overview": "", "genres": [], "rating": 7.0,
                "popularity": 1.0, "lang": "fr",
                "orig_title": "", "alt_titles": []}


class TestEnrichMNFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.db.execute(
            "INSERT INTO titles(kind,title,norm_title,year,added_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            ("series", "Death Note", "death note", 2006, 1, 1))
        self.tid = self.db.query_one("SELECT id FROM titles")["id"]
        self.db.execute(
            "INSERT INTO files(title_id, file_name, tg_chat_id, tg_msg_id,"
            " indexed_at) VALUES(?,?,?,?,1)",
            (self.tid, "Death Note S01E01 720p.mkv", -100, 5))

    def _row(self):
        return self.db.query_one(
            "SELECT title, tmdb_id, poster FROM titles WHERE id=?",
            (self.tid,))

    def test_tmdb_miss_mn_hit_sets_poster_only(self):
        mn = mn_with(DEATH_NOTE_SEARCH)
        idx = Indexer(None, self.db, FakeTMDBNone(), mnimdb=mn)
        asyncio.run(idx._enrich_one(self.tid))
        row = self._row()
        self.assertEqual(row["poster"], POSTER_TV)
        self.assertIsNone(row["tmdb_id"])          # never adopted as a match
        self.assertEqual(row["title"], "Death Note")  # never renamed/merged
        self.assertTrue(self.db.get_kv(f"mnimdb_try:{self.tid}"))

    def test_mn_no_confident_match_marks_tried_keeps_placeholder(self):
        only_film = {"Search": [DEATH_NOTE_SEARCH["Search"][1]],
                     "Response": "True"}
        mn = mn_with(only_film)
        idx = Indexer(None, self.db, FakeTMDBNone(), mnimdb=mn)
        asyncio.run(idx._enrich_one(self.tid))
        self.assertEqual(self._row()["poster"], "")
        self.assertTrue(self.db.get_kv(f"mnimdb_try:{self.tid}"))

    def test_recent_mnimdb_try_skips_http(self):
        self.db.set_kv(f"mnimdb_try:{self.tid}", str(time.time()))
        mn = mn_with(DEATH_NOTE_SEARCH)
        idx = Indexer(None, self.db, FakeTMDBNone(), mnimdb=mn)
        asyncio.run(idx._enrich_one(self.tid))
        self.assertEqual(mn._session.gets, [])

    def test_breaker_open_skips_http(self):
        mn = mn_with(DEATH_NOTE_SEARCH)
        mn.unavailable_until = time.time() + 600
        idx = Indexer(None, self.db, FakeTMDBNone(), mnimdb=mn)
        asyncio.run(idx._enrich_one(self.tid))
        self.assertEqual(mn._session.gets, [])
        self.assertEqual(self._row()["poster"], "")

    def test_tmdb_hit_never_calls_mn(self):
        mn = mn_with(DEATH_NOTE_SEARCH)
        idx = Indexer(None, self.db, FakeTMDBHit(), mnimdb=mn)
        asyncio.run(idx._enrich_one(self.tid))
        self.assertEqual(mn._session.gets, [])
        self.assertEqual(self._row()["poster"], "tmdb-poster")

    def test_no_mnimdb_configured_is_noop(self):
        idx = Indexer(None, self.db, FakeTMDBNone())
        asyncio.run(idx._enrich_one(self.tid))
        self.assertEqual(self._row()["poster"], "")


if __name__ == "__main__":
    unittest.main()
