"""--update source selection: continue the tracked source, auto-refine coarse->fine as the backlog
shrinks, never coarsen, and switch granularity via a clean handoff (one planet source per store)."""

import datetime as dt

import duckdb
import pytest

from osmsg import pipeline
from osmsg.db.schema import create_tables, get_state, upsert_state
from osmsg.pipeline import RunConfig, _select_update_source, _tracked_sources
from osmsg.replication import SHORTCUTS

UTC = dt.UTC
NOW = dt.datetime(2026, 6, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    c = duckdb.connect()
    create_tables(c)
    return c


@pytest.fixture(autouse=True)
def stub_seed(monkeypatch):
    def fake_seed(c, resume_at, url):
        upsert_state(c, source_url=url, last_seq=42, last_ts=resume_at, updated_at=NOW)
        return resume_at

    monkeypatch.setattr(pipeline, "seed_resume_at", fake_seed)


def _track(c, url, last_ts):
    upsert_state(c, source_url=url, last_seq=1, last_ts=last_ts, updated_at=NOW)


def _cfg(urls, explicit):
    return RunConfig(update=True, urls=urls, url_explicit=explicit)


def test_fresh_store_left_untouched(conn):
    cfg = _cfg([SHORTCUTS["minute"]], False)
    _select_update_source(conn, cfg, NOW)
    assert cfg.urls == [SHORTCUTS["minute"]]


def test_auto_continues_current_when_fresh_enough(conn):
    _track(conn, SHORTCUTS["minute"], NOW - dt.timedelta(minutes=2))
    cfg = _cfg([SHORTCUTS["minute"]], False)
    _select_update_source(conn, cfg, NOW)
    assert cfg.urls == [SHORTCUTS["minute"]]
    assert _tracked_sources(conn) == [SHORTCUTS["minute"]]


def test_auto_refines_day_to_hour(conn):
    _track(conn, SHORTCUTS["day"], NOW - dt.timedelta(days=1))
    cfg = _cfg([SHORTCUTS["minute"]], False)
    _select_update_source(conn, cfg, NOW)
    assert cfg.urls == [SHORTCUTS["hour"]]
    assert _tracked_sources(conn) == [SHORTCUTS["hour"]]


def test_auto_never_coarsens(conn):
    _track(conn, SHORTCUTS["minute"], NOW - dt.timedelta(days=7))
    cfg = _cfg([SHORTCUTS["minute"]], False)
    _select_update_source(conn, cfg, NOW)
    assert cfg.urls == [SHORTCUTS["minute"]]
    assert _tracked_sources(conn) == [SHORTCUTS["minute"]]


def test_explicit_switch_day_to_minute_hands_off(conn):
    boundary = NOW - dt.timedelta(days=1)
    _track(conn, SHORTCUTS["day"], boundary)
    cfg = _cfg([SHORTCUTS["minute"]], True)
    _select_update_source(conn, cfg, NOW)
    assert cfg.urls == [SHORTCUTS["minute"]]
    assert _tracked_sources(conn) == [SHORTCUTS["minute"]]
    assert get_state(conn, SHORTCUTS["minute"])["last_ts"] == boundary
