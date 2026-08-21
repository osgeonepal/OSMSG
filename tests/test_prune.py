"""Postgres pruning derives the cutoff from the published frontier minus the overlap buffer."""

import datetime as dt

import pytest

import osmsg.prune as prune
from osmsg.exceptions import OsmsgError
from osmsg.history import Manifest

UTC = dt.UTC


def test_prune_covered_computes_cutoff(monkeypatch):
    frontier = dt.datetime(2026, 8, 1, tzinfo=UTC)
    monkeypatch.setattr(prune, "fetch_manifest", lambda url: Manifest(1, dt.datetime(2005, 4, 1, tzinfo=UTC), frontier))
    captured = {}

    def fake_prune_pg(dsn, cutoff):
        captured["dsn"], captured["cutoff"] = dsn, cutoff
        return (7, 4)

    monkeypatch.setattr(prune, "prune_pg", fake_prune_pg)
    stats_n, cs_n = prune.prune_covered("mydsn", "hf://x", overlap=dt.timedelta(days=2))

    assert captured["cutoff"] == frontier - dt.timedelta(days=2)  # keep 2 days beyond the frontier
    assert captured["dsn"] == "mydsn"
    assert (stats_n, cs_n) == (7, 4)


def test_prune_covered_raises_without_manifest(monkeypatch):
    monkeypatch.setattr(prune, "fetch_manifest", lambda url: None)
    with pytest.raises(OsmsgError):
        prune.prune_covered("dsn", "url")


class _FakeConn:
    def __init__(self, calls):
        self._calls = calls

    def execute(self, sql):
        self._calls.append(sql)
        return self

    def fetchone(self):
        return (5,)

    def close(self):
        pass


def test_prune_pg_deletes_natively_via_postgres_execute(monkeypatch):
    calls = []
    monkeypatch.setattr(prune.duckdb, "connect", lambda *a, **k: _FakeConn(calls))
    stats_n, cs_n = prune.prune_pg("dsn", dt.datetime(2026, 7, 30, tzinfo=UTC))

    deletes = [c for c in calls if "postgres_execute" in c and "DELETE" in c]
    assert len(deletes) == 2  # child (changeset_stats) then parent (changesets)
    assert "changeset_stats" in deletes[0]
    assert "DELETE FROM changesets WHERE" in deletes[1] and "changeset_stats" not in deletes[1]
    assert all("2026-07-30" in d for d in deletes)
    assert (stats_n, cs_n) == (5, 5)
