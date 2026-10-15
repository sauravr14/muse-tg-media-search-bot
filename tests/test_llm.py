"""Tests for the OpenRouter title-identification fallback (mocked HTTP)."""
import asyncio
import json
import os
import tempfile
import unittest

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")

from app.db import DB
from app.indexer import Indexer
from app.llm import LLTitle
from app.tmdb import TMDB


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
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        return FakeResp(self._status, self._payload)

    @property
    def closed(self):
        return False


def llm_with(content):
    payload = {"choices": [{"message": {"content": content}}]}
    return LLTitle("test-key", session=FakeSession(payload))


class TestLLTitle(unittest.TestCase):
    def test_parses_json_guess(self):
        llm = llm_with('{"title": "A Heart in Winter", "year": 1992, "kind": "movie"}')
        g = asyncio.run(llm.guess_title("Un Coeur En Hiver 1992 4K"))
        self.assertEqual(g, {"title": "A Heart in Winter", "year": 1992, "kind": "movie"})

    def test_null_title_gives_none(self):
        llm = llm_with('{"title": null}')
        self.assertIsNone(asyncio.run(llm.guess_title("xyz123")))

    def test_markdown_fence_stripped(self):
        llm = llm_with('```json\n{"title": "F9", "year": 2021, "kind": "movie"}\n```')
        g = asyncio.run(llm.guess_title("Fast Furious 2021"))
        self.assertEqual(g["title"], "F9")

    def test_http_error_gives_none(self):
        llm = LLTitle("k", session=FakeSession({}, status=429))
        self.assertIsNone(asyncio.run(llm.guess_title("abc")))

    def test_no_key_not_available(self):
        self.assertFalse(LLTitle("").available)
        self.assertIsNone(asyncio.run(LLTitle("").guess_title("abc")))

    def test_cache_key_stable(self):
        llm = LLTitle("k")
        self.assertEqual(llm.cache_key("a"), llm.cache_key("a"))
        self.assertNotEqual(llm.cache_key("a"), llm.cache_key("b"))


class FakeTMDB:
    def __init__(self):
        self.calls = []

    async def match(self, p):
        self.calls.append(p.title)
        if p.title == "A Heart in Winter":
            return {"tmdb_id": 42, "kind": "movie", "title": "A Heart in Winter",
                    "year": 1992, "poster": "p", "backdrop": "", "overview": "",
                    "genres": [], "rating": 7.0, "popularity": 1.0, "lang": "fr",
                    "orig_title": "Un cœur en hiver", "alt_titles": []}
        return None


class TestEnrichFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.db.execute(
            "INSERT INTO titles(kind,title,norm_title,year,added_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("movie", "Un Coeur En Hiver", "un coeur en hiver", 1992, 1, 1))
        self.tid = self.db.query_one("SELECT id FROM titles")["id"]
        self.db.execute(
            "INSERT INTO files(title_id, file_name, tg_chat_id, tg_msg_id, indexed_at)"
            " VALUES(?,?,?,?,1)",
            (self.tid, "Un Coeur En Hiver 1992 4K multisub.mkv", -100, 5))

    def test_llm_fallback_adopts_tmdb_meta(self):
        llm = llm_with('{"title": "A Heart in Winter", "year": 1992, "kind": "movie"}')
        idx = Indexer(None, self.db, FakeTMDB(), llm=llm)
        asyncio.run(idx._enrich_one(self.tid))
        row = self.db.query_one("SELECT title, tmdb_id, poster FROM titles WHERE id=?",
                                (self.tid,))
        self.assertEqual(row["title"], "A Heart in Winter")
        self.assertEqual(row["tmdb_id"], 42)
        self.assertEqual(row["poster"], "p")
        # second enrich is a no-op (already has tmdb_id), no extra LLM call
        n = len(llm._session.posts)
        asyncio.run(idx._enrich_one(self.tid))
        self.assertEqual(len(llm._session.posts), n)

    def test_guess_cached_by_filename(self):
        llm = llm_with('{"title": null}')
        idx = Indexer(None, self.db, FakeTMDB(), llm=llm)
        asyncio.run(idx._enrich_one(self.tid))
        self.assertEqual(len(llm._session.posts), 1)
        # clear tmdb_try and retry: cached null guess -> no new HTTP call
        self.db.execute("DELETE FROM kv WHERE key LIKE 'tmdb_try:%'")
        asyncio.run(idx._enrich_one(self.tid))
        self.assertEqual(len(llm._session.posts), 1)


if __name__ == "__main__":
    unittest.main()
