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
