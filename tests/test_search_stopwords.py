"""Stopword stripping for FTS5 prefix searches (2026-10-29).

A lone common-word prefix query ("the", "of") matches a huge slice of
the catalog and takes 15-30s to rank while occupying a reply-executor
thread; users saw searches arrive minutes late. fts_terms() must drop
those words, and callers must return empty results instantly when no
terms remain.
"""
from app.db import fts_terms, SEARCH_STOPWORDS


def test_stopwords_present():
    for w in ("the", "of", "a", "and", "to", "in"):
        assert w in SEARCH_STOPWORDS


def test_lone_stopword_gives_no_terms():
    assert fts_terms("the") == []
    assert fts_terms("The") == []
    assert fts_terms("of the") == []


def test_stopwords_stripped_but_real_terms_kept():
    assert fts_terms("The Death of Robin Hood") == ["Death", "Robin", "Hood"]
    assert fts_terms("dream theatre") == ["dream", "theatre"]
    assert fts_terms("the godfather") == ["godfather"]


def test_quotes_and_spacing_unchanged():
    assert fts_terms('  "dhoom"  ') == ["dhoom"]
    assert fts_terms("") == []
