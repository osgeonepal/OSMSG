"""Hybrid-auto history: window split, manifest parsing/fallback, remote-ingest filter parity, dedup."""

import datetime as dt
import json
from pathlib import Path

import duckdb
import pytest

from osmsg.db.queries import user_stats
from osmsg.db.schema import create_tables
from osmsg.history import (
    Manifest,
    RemoteFilters,
    fetch_manifest,
    ingest_remote,
    split_window,
)

UTC = dt.UTC


def _utc(y, m, d):
    return dt.datetime(y, m, d, tzinfo=UTC)


# --- window split ----------------------------------------------------------------------------


def _manifest(min_month="2010-01", max_month="2024-01"):
    from osmsg.history import _month_start, _next_month

    return Manifest(schema_version=1, min_month=_month_start(min_month), frontier=_next_month(_month_start(max_month)))


def test_split_fully_covered():
    s = split_window(_utc(2015, 1, 1), _utc(2016, 1, 1), _manifest())
    assert s.has_remote
    assert s.remote_start == _utc(2015, 1, 1) and s.remote_end == _utc(2016, 1, 1)
    assert s.live_start == _utc(2016, 1, 1)  # nothing left for live


def test_split_partial_tail():
    # dataset ends 2024-01; window runs into 2024-03 -> remote to 2024-02-01, live from there
    s = split_window(_utc(2023, 12, 1), _utc(2024, 3, 1), _manifest())
    assert s.has_remote
    assert s.remote_end == _utc(2024, 2, 1)
    assert s.live_start == _utc(2024, 2, 1)


def test_split_uncovered_recent():
    s = split_window(_utc(2024, 5, 1), _utc(2024, 6, 1), _manifest())
    assert not s.has_remote
    assert s.live_start == _utc(2024, 5, 1)  # all live


def test_split_clamps_before_min_month():
    s = split_window(_utc(2008, 1, 1), _utc(2011, 1, 1), _manifest(min_month="2010-01"))
    assert s.remote_start == _utc(2010, 1, 1)  # clamped up to dataset start


def test_live_start_backsteps_at_frontier():
    # a query reaching the published frontier re-scans the safety window on live, so a short final
    # month is recovered by the seq_id=0 dedup instead of silently missed
    from osmsg.history import RESUME_SAFETY
    from osmsg.pipeline import _history_live_start

    m = _manifest(max_month="2024-01")  # frontier 2024-02-01
    reaching = split_window(_utc(2023, 12, 1), _utc(2024, 3, 1), m)  # remote_end == frontier
    assert _history_live_start(reaching, m.frontier) == m.frontier - RESUME_SAFETY
    interior = split_window(_utc(2015, 1, 1), _utc(2016, 1, 1), m)  # remote_end well below frontier
    assert _history_live_start(interior, m.frontier) == interior.live_start  # no backstep


# --- manifest fetch + fallback ---------------------------------------------------------------


def test_fetch_manifest_local(tmp_path: Path):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "min_month": "2010-01", "max_month": "2024-01"})
    )
    m = fetch_manifest(str(tmp_path))
    assert m is not None and m.min_month == _utc(2010, 1, 1) and m.frontier == _utc(2024, 2, 1)


def test_fetch_manifest_missing_returns_none(tmp_path: Path):
    assert fetch_manifest(str(tmp_path)) is None


def test_fetch_manifest_schema_mismatch_returns_none(tmp_path: Path):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema_version": 999, "min_month": "2010-01", "max_month": "2024-01"})
    )
    assert fetch_manifest(str(tmp_path)) is None


def test_fetch_manifest_malformed_returns_none(tmp_path: Path):
    (tmp_path / "manifest.json").write_text("{not json")
    assert fetch_manifest(str(tmp_path)) is None


# --- remote ingest: build a tiny published dataset, ingest, assert parity --------------------

_CHANGESETS = [
    # id, uid, username, created, editor, hashtags, bbox(min_lon,min_lat,max_lon,max_lat)
    (100, 1, "alice", "2024-01-10", "JOSM", ["#hotosm-project-1"], (13.0, 52.3, 13.2, 52.5)),
    (200, 2, "bob", "2024-01-15", "iD", [], (0.0, 0.0, 0.1, 0.1)),
    (300, 1, "alice", "2024-01-20", "JOSM", ["#missingmaps"], (13.1, 52.4, 13.3, 52.6)),
]
# id, nodes_created, ways_created, tag_stats
_CHANGEFILES = [
    (100, 5, 1, '{"building":{"yes":{"c":1,"m":0}}}'),
    (200, 3, 0, None),
    (300, 0, 2, '{"highway":{"residential":{"c":2,"m":0}}}'),
]


def _build_dataset(root: Path):
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    cs_vals = ",\n".join(
        f"({cid},{uid},'{name}',TIMESTAMPTZ '{cre}','{ed}',{hts},"
        f"{(b[0] + b[2]) / 2},{(b[1] + b[3]) / 2},{b[0]},{b[1]},{b[2]},{b[3]})"
        for (cid, uid, name, cre, ed, ht, b) in _CHANGESETS
        for hts in [("[" + ",".join(f"'{h}'" for h in ht) + "]") if ht else "[]"]
    )
    cs_dir = root / "changesets" / "year=2024" / "month=1"
    cs_dir.mkdir(parents=True)
    con.execute(
        f"""COPY (SELECT * FROM (VALUES {cs_vals})
            AS t(changeset_id,uid,username,created_at,editor,hashtags,lon,lat,min_lon,min_lat,max_lon,max_lat))
            TO '{(cs_dir / "data.parquet").as_posix()}' (FORMAT parquet)"""
    )
    # full changeset_stats count set; only nodes_created/ways_created vary, rest are 0
    cf_vals = ",\n".join(
        f"({cid},{uid},{nc},0,0,{wc},0,0,0,0,0,0,0,"
        f"{('NULL' if ts is None else chr(39) + ts + chr(39))},TIMESTAMPTZ '{cre}',0,0,0,0,0,0)"
        for (cid, nc, wc, ts), (_, uid, _n, cre, _e, _h, _b) in zip(_CHANGEFILES, _CHANGESETS, strict=True)
    )
    cf_cols = (
        "changeset_id,uid,nodes_created,nodes_modified,nodes_deleted,ways_created,ways_modified,ways_deleted,"
        "rels_created,rels_modified,rels_deleted,poi_created,poi_modified,tag_stats,created_at,"
        "lon,lat,min_lon,min_lat,max_lon,max_lat"
    )
    cf_dir = root / "changefiles" / "year=2024" / "month=1"
    cf_dir.mkdir(parents=True)
    con.execute(
        f"""COPY (SELECT * EXCLUDE (tag_stats), tag_stats::JSON tag_stats FROM (VALUES {cf_vals}) AS t({cf_cols}))
            TO '{(cf_dir / "data.parquet").as_posix()}' (FORMAT parquet)"""
    )
    con.close()
    (root / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "min_month": "2024-01", "max_month": "2024-01"})
    )


def _ingest(tmp_path: Path, filters: RemoteFilters) -> duckdb.DuckDBPyConnection:
    _build_dataset(tmp_path)
    conn = duckdb.connect()
    create_tables(conn)
    split = split_window(_utc(2024, 1, 1), _utc(2024, 2, 1), fetch_manifest(str(tmp_path)))
    ingest_remote(conn, split, filters, str(tmp_path))
    return conn


def _by_name(rows):
    return {r["name"]: r for r in rows}


def test_ingest_no_filter_parity(tmp_path: Path):
    conn = _ingest(tmp_path, RemoteFilters(None, False, None, None))
    rows = _by_name(user_stats(conn))
    assert rows["alice"]["nodes_create"] == 5 and rows["alice"]["ways_create"] == 3  # cs100 + cs300
    assert rows["bob"]["nodes_create"] == 3
    # changeset_stats carries history sentinel seq_id
    assert conn.execute("SELECT count(*) FROM changeset_stats WHERE seq_id=0").fetchone()[0] == 3


def test_ingest_populates_changesets_for_fk(tmp_path: Path):
    # Even a no-filter run must populate changesets for every changeset_stats row, or Postgres's
    # changeset_stats -> changesets foreign key is violated on export (DuckDB has no FK to catch it).
    conn = _ingest(tmp_path, RemoteFilters(None, False, None, None))
    orphans = conn.execute(
        "SELECT count(*) FROM changeset_stats s LEFT JOIN changesets c USING (changeset_id) "
        "WHERE c.changeset_id IS NULL"
    ).fetchone()[0]
    assert orphans == 0


def test_ingest_hashtag_filter(tmp_path: Path):
    # substring 'hotosm' matches '#hotosm-project-1' (cs100) only
    conn = _ingest(tmp_path, RemoteFilters(["#hotosm"], False, None, None))
    rows = _by_name(user_stats(conn))
    assert set(rows) == {"alice"}
    assert rows["alice"]["nodes_create"] == 5 and rows["alice"]["ways_create"] == 1  # only cs100


def test_ingest_boundary_filter(tmp_path: Path):
    berlin = "POLYGON((12.9 52.2, 13.5 52.2, 13.5 52.7, 12.9 52.7, 12.9 52.2))"
    conn = _ingest(tmp_path, RemoteFilters(None, False, None, berlin))
    rows = _by_name(user_stats(conn))
    assert set(rows) == {"alice"}  # cs100 + cs300 are in Berlin; cs200 (bob) at (0,0) excluded
    assert rows["alice"]["ways_create"] == 3


def test_ingest_users_filter(tmp_path: Path):
    conn = _ingest(tmp_path, RemoteFilters(None, False, ["bob"], None))
    rows = _by_name(user_stats(conn))
    assert set(rows) == {"bob"} and rows["bob"]["nodes_create"] == 3


def test_history_dedup_drops_live_duplicate(tmp_path: Path):
    conn = _ingest(tmp_path, RemoteFilters(None, False, None, None))
    # simulate a live row (seq_id=5) for a changeset the history already owns (cs100)
    conn.execute("INSERT INTO changeset_stats VALUES (100,5,1,99,0,0,0,0,0,0,0,0,0,0,NULL)")
    conn.execute(
        """DELETE FROM changeset_stats WHERE seq_id<>0
           AND changeset_id IN (SELECT changeset_id FROM changeset_stats WHERE seq_id=0)"""
    )
    rows = _by_name(user_stats(conn))
    # alice's cs100 nodes stay 5 (history), not 5+99 -> live dup removed, no double count
    assert rows["alice"]["nodes_create"] == 5


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_update_auto_seeds_resume_from_history(tmp_path, monkeypatch):
    """--update on a store loaded from history (seq_id=0 rows) but with no state seeds the resume
    point from the frontier, instead of the default bootstrap window."""
    import osmsg.pipeline as pipeline
    from osmsg.db.schema import create_tables
    from osmsg.pipeline import RunConfig

    conn = duckdb.connect(str(tmp_path / "s.duckdb"))
    create_tables(conn)
    conn.execute("INSERT INTO changeset_stats VALUES (1, 0, 1, 5,0,0, 0,0,0, 0,0,0, 0,0, NULL)")

    seeded = []
    monkeypatch.setattr(pipeline, "seed_resume_state", lambda c, hurl, url: seeded.append(url))

    url = "https://planet.openstreetmap.org/replication/day"
    pipeline._seed_history_resume(conn, RunConfig(update=True, urls=[url], history_mode="auto"))
    assert seeded == [url]  # seeded because history rows exist and no state yet


def test_update_no_seed_without_history(tmp_path, monkeypatch):
    import osmsg.pipeline as pipeline
    from osmsg.db.schema import create_tables
    from osmsg.pipeline import RunConfig

    conn = duckdb.connect(str(tmp_path / "s.duckdb"))
    create_tables(conn)  # empty: no history rows

    seeded = []
    monkeypatch.setattr(pipeline, "seed_resume_state", lambda c, hurl, url: seeded.append(url))
    pipeline._seed_history_resume(conn, RunConfig(update=True, urls=["x"], history_mode="auto"))
    assert seeded == []  # nothing loaded -> no seed, falls back to normal bootstrap


def test_ingest_retries_transient_read(tmp_path, monkeypatch):
    import osmsg.history as history

    _build_dataset(tmp_path)
    conn = duckdb.connect()
    create_tables(conn)
    real = history._partition_list
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise duckdb.IOException("transient read")
        return real(*args, **kwargs)

    monkeypatch.setattr(history, "_partition_list", flaky)
    monkeypatch.setattr(history.time, "sleep", lambda _s: None)
    split = split_window(_utc(2024, 1, 1), _utc(2024, 2, 1), fetch_manifest(str(tmp_path)))
    n = ingest_remote(conn, split, RemoteFilters(None, False, None, None), str(tmp_path))
    assert n == 3 and calls["n"] >= 2


def test_ingest_gives_up_after_retries(tmp_path, monkeypatch):
    import osmsg.history as history

    _build_dataset(tmp_path)
    conn = duckdb.connect()
    create_tables(conn)

    def always_fail(*args, **kwargs):
        raise duckdb.IOException("down")

    monkeypatch.setattr(history, "_partition_list", always_fail)
    monkeypatch.setattr(history.time, "sleep", lambda _s: None)
    split = split_window(_utc(2024, 1, 1), _utc(2024, 2, 1), fetch_manifest(str(tmp_path)))
    with pytest.raises(duckdb.Error):
        ingest_remote(conn, split, RemoteFilters(None, False, None, None), str(tmp_path))
