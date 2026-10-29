"""Nightly enrichment window (2026-10-29): the enricher runs 01:00-06:00
IST only, so daytime user traffic gets both vCPUs."""
from datetime import datetime

from app.indexer import ENRICH_TZ, Indexer


def _at(h, m=0):
    return datetime(2026, 10, 29, h, m, tzinfo=ENRICH_TZ)


def _pause(h, m=0):
    return Indexer._enrich_pause_seconds(None, _at(h, m))


def test_window_open_inside():
    assert _pause(1, 0) == 0.0
    assert _pause(3, 30) == 0.0
    assert _pause(5, 59) == 0.0


def test_window_closed_edges():
    assert _pause(0, 59) == 60.0
    assert _pause(6, 0) > 0


def test_pause_lengths():
    assert _pause(0, 30) == 1800.0
    assert _pause(12, 0) == 13 * 3600.0
    assert _pause(23, 0) == 2 * 3600.0
    assert _pause(6, 30) == 18.5 * 3600.0
