"""Tests for the FastAPI catalog API + WebApp initData validation."""
import hashlib
import hmac
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app, validate_init_data
from app.classify import Classifier
from app.config import Config
from app.db import DB
from app.parser import parse_filename


def _seed(db: DB):
    clf = Classifier(db)
    meta = lambda c, m, n="f.mkv": {"file_name": n, "size": 10, "duration": 5,
                                    "mime": "video/x-matroska", "tg_chat_id": c,
                                    "tg_msg_id": m, "caption": "", "msg_date": 0}
    for i, r in enumerate(["480p", "1080p"]):
        clf.ingest(parse_filename(f"Inception.2010.{r}.mkv"), meta(-1001, 1 + i, f"Inception.2010.{r}.mkv"))
    for i, e in enumerate(["S01E01", "S01E02"]):
        clf.ingest(parse_filename(f"Dark.{e}.720p.mkv"), meta(-1001, 10 + i, f"Dark.{e}.720p.mkv"))


def _signed_init_data(bot_token: str, user_id: int = 99) -> str:
    pairs = {"user": json.dumps({"id": user_id}),
             "auth_date": "1792081000", "query_id": "abc"}
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    pairs["hash"] = sig
    return urllib.parse.urlencode(pairs)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = DB(Path(tempfile.mkdtemp()) / "api.db")
        _seed(cls.db)
        cfg = Config()
        cfg.bot_token = "test-token"
        cfg.admin_ids = (99,)
        cls.client = TestClient(create_app(cfg, cls.db, bot_username="testbot"))

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})

    def test_search_groups_movie(self):
        r = self.client.get("/api/search", params={"q": "Inception"})
        d = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(d["results"]), 1)  # one card, not per quality
        self.assertEqual(d["results"][0]["title"], "Inception")

    def test_search_series(self):
        d = self.client.get("/api/search", params={"q": "Dark"}).json()
        self.assertEqual(len(d["results"]), 1)
        self.assertEqual(d["results"][0]["kind"], "series")

    def test_search_empty(self):
        d = self.client.get("/api/search", params={"q": "zzzznope"}).json()
        self.assertEqual(d["results"], [])

    def test_recent(self):
        d = self.client.get("/api/recent").json()
        self.assertEqual(len(d["results"]), 2)

    def test_home_shape(self):
        # give Inception full TMDB dressing so it qualifies for hero + rails
        self.db.execute(
            "UPDATE titles SET poster='p.jpg', backdrop='b.jpg',"
            " overview='A dream heist.' WHERE title='Inception'")
        d = self.client.get("/api/home").json()
        self.assertEqual([h["title"] for h in d["hero"]], ["Inception"])
        self.assertEqual(d["hero"][0]["backdrop"], "b.jpg")
        rails = {r["title"]: r["results"] for r in d["rows"]}
        self.assertIn("Recently added movies", rails)
        self.assertEqual([t["title"] for t in rails["Recently added movies"]],
                         ["Inception"])
        # poster-less titles (Dark) stay out of the poster rails
        self.assertNotIn("Recently added series", rails)

    def test_movie_detail_versions(self):
        # note: test_admin_hide runs first alphabetically and hides the 1080p file
        tid = self.client.get("/api/search", params={"q": "Inception"}).json()["results"][0]["id"]
        d = self.client.get(f"/api/title/{tid}").json()
        self.assertEqual([v["resolution"] for v in d["versions"]], ["480p"])

    def test_series_detail_structure(self):
        tid = self.client.get("/api/search", params={"q": "Dark"}).json()["results"][0]["id"]
        d = self.client.get(f"/api/title/{tid}").json()
        self.assertEqual(len(d["seasons"]), 1)
        self.assertEqual(len(d["seasons"][0]["episodes"]), 2)
        ep1 = d["seasons"][0]["episodes"][0]
        self.assertEqual(ep1["ep_no"], 1)
        self.assertEqual(len(ep1["versions"]), 1)

    def test_deeplink(self):
        tid = self.client.get("/api/search", params={"q": "Inception"}).json()["results"][0]["id"]
        fid = self.client.get(f"/api/title/{tid}").json()["versions"][0]["id"]
        d = self.client.get(f"/api/deeplink/file/{fid}").json()
        self.assertTrue(d["url"].startswith("https://t.me/testbot?start=file_"))

    def test_init_data_valid(self):
        init = _signed_init_data("test-token", 99)
        self.assertIsNotNone(validate_init_data(init, "test-token"))

    def test_init_data_tampered(self):
        init = _signed_init_data("test-token", 99).replace("id%22%3A+99", "id%22%3A+100")
        self.assertIsNone(validate_init_data(init, "test-token"))

    def test_init_data_wrong_token(self):
        init = _signed_init_data("test-token", 99)
        self.assertIsNone(validate_init_data(init, "other-token"))

    def test_admin_denied_without_init(self):
        r = self.client.get("/api/admin/overview")
        self.assertEqual(r.status_code, 403)

    def test_admin_denied_non_admin(self):
        init = _signed_init_data("test-token", 100)  # not in admin_ids
        r = self.client.get("/api/admin/overview", headers={"x-telegram-init-data": init})
        self.assertEqual(r.status_code, 403)

    def test_admin_allowed(self):
        init = _signed_init_data("test-token", 99)
        r = self.client.get("/api/admin/overview", headers={"x-telegram-init-data": init})
        self.assertEqual(r.status_code, 200)
        self.assertIn("stats", r.json())

    def test_admin_hide(self):
        init = _signed_init_data("test-token", 99)
        tid = self.client.get("/api/search", params={"q": "Inception"}).json()["results"][0]["id"]
        fid = self.client.get(f"/api/title/{tid}").json()["versions"][0]["id"]
        r = self.client.post(f"/api/admin/file/{fid}/hide",
                             headers={"x-telegram-init-data": init})
        self.assertEqual(r.status_code, 200)
        d = self.client.get(f"/api/title/{tid}").json()
        self.assertEqual(len(d["versions"]), 1)  # hidden one gone


class MultiWordSearchTest(unittest.TestCase):
    """Regression (2026-10-22): /api/search built its FTS5 query as
    adjacent parenthesised term groups — a syntax error FTS5 rejects,
    silently swallowed into an empty result list. Every multi-word
    search ("black panther", "the good doctor") returned nothing while
    single words worked. Groups must be joined with an explicit AND."""

    @classmethod
    def setUpClass(cls):
        cls.db = DB(Path(tempfile.mkdtemp()) / "api2.db")
        clf = Classifier(cls.db)
        meta = lambda c, m, n: {"file_name": n, "size": 10, "duration": 5,
                                "mime": "video/x-matroska", "tg_chat_id": c,
                                "tg_msg_id": m, "caption": "", "msg_date": 0}
        names = ["Black.Panther.2018.1080p.mkv",
                 "The.Good.Doctor.S01E01.720p.mkv",
                 "House.of.the.Dragon.S01E01.720p.mkv"]
        for i, n in enumerate(names):
            clf.ingest(parse_filename(n), meta(-1002, 1 + i, n))
        cfg = Config()
        cfg.bot_token = "test-token"
        cls.client = TestClient(create_app(cfg, cls.db, bot_username="testbot"))

    def _titles(self, q):
        d = self.client.get("/api/search", params={"q": q}).json()
        return [r["title"] for r in d["results"]]

    def test_two_word_movie(self):
        self.assertEqual(self._titles("black panther"), ["Black Panther"])

    def test_three_word_series_with_the(self):
        self.assertEqual(self._titles("the good doctor"), ["The Good Doctor"])

    def test_multi_word_plural_term(self):
        self.assertEqual(self._titles("house of the dragons"),
                         ["House Of The Dragon"])  # parser title-cases

    def test_partial_phrase_still_matches(self):
        self.assertEqual(self._titles("good doctor"), ["The Good Doctor"])


if __name__ == "__main__":
    unittest.main()
