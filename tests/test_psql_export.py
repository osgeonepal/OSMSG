"""Schema validation for the PostgreSQL exporter.

A live push test requires a Postgres instance and is gated behind the
`OSMSG_PG_DSN` env var (mark `network` to deselect by default).
"""

from __future__ import annotations

import os
import re

import duckdb
import pyarrow.parquet as pq
import pytest

from osmsg.db.queries import user_stats
from osmsg.export.parquet import to_parquet
from osmsg.export.psql import PG_SCHEMA, to_psql


def test_pg_schema_contains_every_osmsg_table():
    """PG_SCHEMA must declare the full osmsg schema (no silent regression)."""
    statements = [s for s in PG_SCHEMA.strip().split(";") if s.strip()]
    table_names = {
        m.group(1)
        for s in statements
        for m in [re.search(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", s, re.IGNORECASE)]
        if m
    }
    assert {"users", "changesets", "changeset_stats", "state"} <= table_names


def test_pg_schema_uses_jsonb_for_tag_stats():
    """JSONB (binary, indexable) is the right PG type for tag_stats."""
    assert "tag_stats      JSONB" in PG_SCHEMA


def test_pg_schema_state_is_single_row_per_source():
    """`state` is keyed by source_url alone — one row per replication source, ever.
    The PSQL exporter UPSERTs on conflict so every osmsg run keeps PG in sync."""
    assert "source_url  TEXT PRIMARY KEY" in PG_SCHEMA
    assert "BIGSERIAL" not in PG_SCHEMA  # no synthetic ids needed


def test_pg_schema_statements_each_parse_with_postgres_extension():
    """Each individual CREATE statement is well-formed enough that the postgres
    extension's parser would accept it — we use DuckDB's own parser as an
    approximation (DuckDB's CREATE TABLE syntax is compatible)."""
    duckdb_clone = (
        PG_SCHEMA.replace("DOUBLE PRECISION", "DOUBLE")
        .replace("JSONB", "JSON")
        .replace("TEXT", "VARCHAR")
    )
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    for stmt in [s.strip() for s in duckdb_clone.split(";") if s.strip()]:
        upper = stmt.upper()
        if "USING GIN" in upper:
            continue
        conn.execute(stmt)
    tables = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert {"users", "changesets", "changeset_stats", "state"} <= tables


EXPECTED_USER_STATS = {
    "alice": {"changesets": 1, "nodes_create": 30, "ways_create": 8, "poi_create": 5, "map_changes": 44},
    "bob": {"changesets": 1, "nodes_create": 50, "ways_create": 0, "poi_create": 50, "map_changes": 50},
}


def _assert_user_stats_match(actual: list[dict], expected: dict[str, dict[str, int]]) -> None:
    by_name = {r["name"]: r for r in actual}
    assert set(by_name) == set(expected), f"users mismatch: {set(by_name)} vs {set(expected)}"
    for name, fields in expected.items():
        for col, want in fields.items():
            assert by_name[name][col] == want, f"{name}.{col}: got {by_name[name][col]} want {want}"


def test_duckdb_user_stats_match_seed_data(fresh_db, populated_db_factory):
    """Anchor for EXPECTED_USER_STATS — if it drifts, every other roundtrip test silently lies."""
    rows = user_stats(populated_db_factory(fresh_db))
    _assert_user_stats_match(rows, EXPECTED_USER_STATS)


def test_user_stats_roundtrip_through_parquet(tmp_path, fresh_db, populated_db_factory):
    rows = user_stats(populated_db_factory(fresh_db))
    out = to_parquet(rows, tmp_path / "stats.parquet")

    table = pq.read_table(out).to_pylist()
    _assert_user_stats_match(table, EXPECTED_USER_STATS)


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("OSMSG_PG_DSN"), reason="OSMSG_PG_DSN not set; live PG push not exercised")
def test_user_stats_roundtrip_through_postgres(fresh_db, populated_db_factory):
    populated = populated_db_factory(fresh_db)
    dsn = os.environ["OSMSG_PG_DSN"]

    populated.execute("INSTALL postgres")
    populated.execute("LOAD postgres")
    safe_dsn = dsn.replace("'", "''")
    populated.execute(f"ATTACH '{safe_dsn}' AS pg_wipe (TYPE postgres)")
    try:
        for stmt in PG_SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                populated.execute(f"CALL postgres_execute('pg_wipe', $${stmt}$$)")
        for table in ("changeset_stats", "changesets", "users", "state"):
            populated.execute(f"CALL postgres_execute('pg_wipe', $$DELETE FROM {table}$$)")
    finally:
        populated.execute("DETACH pg_wipe")

    to_psql(populated, dsn)

    verifier = duckdb.connect(":memory:")
    verifier.execute("INSTALL postgres")
    verifier.execute("LOAD postgres")
    verifier.execute(f"ATTACH '{safe_dsn}' AS pg_src (TYPE postgres, READ_ONLY)")
    try:
        rows = verifier.execute(
            """
            SELECT u.username AS name,
                   COUNT(DISTINCT cs.changeset_id) AS changesets,
                   SUM(cs.nodes_created) AS nodes_create,
                   SUM(cs.ways_created)  AS ways_create,
                   SUM(cs.poi_created)   AS poi_create,
                   SUM(
                       cs.nodes_created + cs.nodes_modified + cs.nodes_deleted +
                       cs.ways_created  + cs.ways_modified  + cs.ways_deleted  +
                       cs.rels_created  + cs.rels_modified  + cs.rels_deleted
                   ) AS map_changes
            FROM pg_src.users u
            JOIN pg_src.changeset_stats cs ON u.uid = cs.uid
            GROUP BY u.username
            """
        ).fetchall()
    finally:
        verifier.execute("DETACH pg_src")
        verifier.close()

    cols = ("name", "changesets", "nodes_create", "ways_create", "poi_create", "map_changes")
    actual = [dict(zip(cols, r, strict=True)) for r in rows]
    _assert_user_stats_match(actual, EXPECTED_USER_STATS)


def test_merge_parquet_upgrades_empty_changeset_when_richer_data_arrives(fresh_db, tmp_path):
    """Empty stub from tick 1 must be upgraded to richer data when tick 2 arrives."""
    import json as _json

    from osmsg.db.ingest import flush_rows_to_parquet, merge_parquet_files

    flush_rows_to_parquet(
        parquet_dir=tmp_path / "round1",
        pid=1,
        batch_index=0,
        users=[(99, "lexoa")],
        changesets=[(182308935, 99, None, None, None, None, None, None, None)],
        changeset_stats=[],
    )
    merge_parquet_files(fresh_db, tmp_path / "round1", cleanup=True)

    row = fresh_db.execute(
        "SELECT geom IS NULL, editor, hashtags FROM changesets WHERE changeset_id = 182308935"
    ).fetchone()
    assert row == (True, None, None), f"round 1 expected empty stub, got {row}"

    flush_rows_to_parquet(
        parquet_dir=tmp_path / "round2",
        pid=2,
        batch_index=0,
        users=[(99, "lexoa")],
        changesets=[(182308935, 99, None, ["#mapathon"], "iD", 85.0, 27.0, 85.5, 27.5)],
        changeset_stats=[
            (
                182308935,
                5000,
                99,
                10,
                0,
                0,
                3,
                0,
                0,
                0,
                0,
                0,
                5,
                0,
                _json.dumps({"building": {"yes": {"c": 3, "m": 0}}}),
            )
        ],
    )
    merge_parquet_files(fresh_db, tmp_path / "round2", cleanup=True)

    geom_wkt, editor, hashtags = fresh_db.execute(
        "SELECT ST_AsText(geom), editor, hashtags FROM changesets WHERE changeset_id = 182308935"
    ).fetchone()
    assert "POLYGON" in geom_wkt
    assert editor == "iD"
    assert hashtags == ["#mapathon"]

    stats = fresh_db.execute(
        "SELECT COUNT(*), SUM(nodes_created) FROM changeset_stats WHERE changeset_id = 182308935"
    ).fetchone()
    assert stats == (1, 10)


def test_merge_parquet_keeps_existing_geom_when_new_row_has_null(fresh_db, tmp_path):
    """A NULL src column must not overwrite existing non-NULL data."""
    from osmsg.db.ingest import flush_rows_to_parquet, merge_parquet_files

    flush_rows_to_parquet(
        parquet_dir=tmp_path / "rich",
        pid=1,
        batch_index=0,
        users=[(99, "lexoa")],
        changesets=[(900, 99, None, ["#a"], "iD", 1.0, 2.0, 3.0, 4.0)],
        changeset_stats=[],
    )
    merge_parquet_files(fresh_db, tmp_path / "rich", cleanup=True)

    flush_rows_to_parquet(
        parquet_dir=tmp_path / "stub",
        pid=2,
        batch_index=0,
        users=[(99, "lexoa")],
        changesets=[(900, 99, None, None, None, None, None, None, None)],
        changeset_stats=[],
    )
    merge_parquet_files(fresh_db, tmp_path / "stub", cleanup=True)

    geom_wkt, editor, hashtags = fresh_db.execute(
        "SELECT ST_AsText(geom), editor, hashtags FROM changesets WHERE changeset_id = 900"
    ).fetchone()
    assert "POLYGON" in geom_wkt
    assert editor == "iD"
    assert hashtags == ["#a"]


def test_merge_parquet_replaces_partial_geom_when_richer_arrives(fresh_db, tmp_path):
    """OSM bbox grows monotonically across re-emits; later tick must overwrite earlier partial bbox."""
    from osmsg.db.ingest import flush_rows_to_parquet, merge_parquet_files

    flush_rows_to_parquet(
        parquet_dir=tmp_path / "partial",
        pid=1,
        batch_index=0,
        users=[(99, "lexoa")],
        changesets=[(901, 99, None, None, "iD", 10.0, 10.0, 10.5, 10.5)],
        changeset_stats=[],
    )
    merge_parquet_files(fresh_db, tmp_path / "partial", cleanup=True)

    flush_rows_to_parquet(
        parquet_dir=tmp_path / "final",
        pid=2,
        batch_index=0,
        users=[(99, "lexoa")],
        changesets=[(901, 99, None, ["#mapathon"], "iD", 10.0, 10.0, 12.0, 12.0)],
        changeset_stats=[],
    )
    merge_parquet_files(fresh_db, tmp_path / "final", cleanup=True)

    geom_wkt, hashtags = fresh_db.execute(
        "SELECT ST_AsText(geom), hashtags FROM changesets WHERE changeset_id = 901"
    ).fetchone()
    assert "12 12" in geom_wkt, f"expected final bbox with 12,12 corner, got {geom_wkt}"
    assert hashtags == ["#mapathon"]


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("OSMSG_PG_DSN"), reason="OSMSG_PG_DSN not set; live PG push not exercised")
def test_to_psql_upgrades_empty_changeset_when_pushed_again(fresh_db, tmp_path):
    """Same empty-then-rich scenario across two to_psql() calls into PG."""
    import json as _json

    from osmsg.db.ingest import flush_rows_to_parquet, merge_parquet_files

    dsn = os.environ["OSMSG_PG_DSN"]
    safe_dsn = dsn.replace("'", "''")

    fresh_db.execute("INSTALL postgres")
    fresh_db.execute("LOAD postgres")
    fresh_db.execute(f"ATTACH '{safe_dsn}' AS pg_w (TYPE postgres)")
    try:
        for stmt in PG_SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                fresh_db.execute(f"CALL postgres_execute('pg_w', $${stmt}$$)")
        for table in ("changeset_stats", "changesets", "users", "state"):
            fresh_db.execute(f"CALL postgres_execute('pg_w', $$DELETE FROM {table}$$)")
    finally:
        fresh_db.execute("DETACH pg_w")

    flush_rows_to_parquet(
        parquet_dir=tmp_path / "r1",
        pid=1,
        batch_index=0,
        users=[(77, "carol")],
        changesets=[(900900, 77, None, None, None, None, None, None, None)],
        changeset_stats=[],
    )
    merge_parquet_files(fresh_db, tmp_path / "r1", cleanup=True)
    to_psql(fresh_db, dsn)

    flush_rows_to_parquet(
        parquet_dir=tmp_path / "r2",
        pid=2,
        batch_index=0,
        users=[(77, "carol")],
        changesets=[(900900, 77, None, ["#x"], "JOSM", 10.0, 20.0, 11.0, 21.0)],
        changeset_stats=[
            (900900, 6000, 77, 5, 0, 0, 0, 0, 0, 0, 0, 0, 5, 0, _json.dumps({"shop": {"bakery": {"c": 1, "m": 0}}})),
        ],
    )
    merge_parquet_files(fresh_db, tmp_path / "r2", cleanup=True)
    to_psql(fresh_db, dsn)

    verifier = duckdb.connect(":memory:")
    verifier.execute("INSTALL postgres")
    verifier.execute("LOAD postgres")
    verifier.execute(f"ATTACH '{safe_dsn}' AS pg_src (TYPE postgres, READ_ONLY)")
    try:
        editor, hashtags, min_lon, min_lat, max_lon, max_lat = verifier.execute(
            """
            SELECT editor, hashtags, min_lon, min_lat, max_lon, max_lat
            FROM pg_src.changesets
            WHERE changeset_id = 900900
            """
        ).fetchone()
        n_stats = verifier.execute(
            "SELECT COUNT(*) FROM pg_src.changeset_stats WHERE changeset_id = 900900"
        ).fetchone()[0]
    finally:
        verifier.execute("DETACH pg_src")
        verifier.close()

    assert editor == "JOSM"
    assert hashtags == ["#x"]
    assert (min_lon, min_lat, max_lon, max_lat) == (10.0, 20.0, 11.0, 21.0)
    assert n_stats == 1


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("OSMSG_PG_DSN"), reason="OSMSG_PG_DSN not set; live PG push not exercised")
def test_to_psql_refuses_when_pg_has_data_from_a_different_source(fresh_db, populated_db_factory):
    """Pushing source B to a PG that already has source A's state must hard-error."""
    import datetime as _dt

    from osmsg.exceptions import OsmsgError

    dsn = os.environ["OSMSG_PG_DSN"]
    safe_dsn = dsn.replace("'", "''")

    populated = populated_db_factory(fresh_db)
    populated.execute("INSTALL postgres")
    populated.execute("LOAD postgres")
    populated.execute(f"ATTACH '{safe_dsn}' AS pg_w (TYPE postgres)")
    try:
        for stmt in PG_SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                populated.execute(f"CALL postgres_execute('pg_w', $${stmt}$$)")
        for table in ("changeset_stats", "changesets", "users", "state"):
            populated.execute(f"CALL postgres_execute('pg_w', $$DELETE FROM {table}$$)")
    finally:
        populated.execute("DETACH pg_w")

    populated.execute(
        "INSERT INTO state VALUES (?, ?, ?, ?)",
        [
            "https://download.geofabrik.de/asia/nepal-updates",
            100,
            _dt.datetime(2026, 5, 1, tzinfo=_dt.UTC),
            _dt.datetime(2026, 5, 1, tzinfo=_dt.UTC),
        ],
    )
    to_psql(populated, dsn)

    populated.execute("DELETE FROM state")
    populated.execute(
        "INSERT INTO state VALUES (?, ?, ?, ?)",
        [
            "https://planet.openstreetmap.org/replication/minute",
            7000000,
            _dt.datetime(2026, 5, 7, tzinfo=_dt.UTC),
            _dt.datetime(2026, 5, 7, tzinfo=_dt.UTC),
        ],
    )

    with pytest.raises(OsmsgError, match="Mixing sources"):
        to_psql(populated, dsn)


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("OSMSG_PG_DSN"), reason="OSMSG_PG_DSN not set; live PG push not exercised")
def test_to_psql_allows_repush_from_same_source(fresh_db, populated_db_factory):
    """A second push from the SAME source URL is fine — common --update path."""
    import datetime as _dt

    dsn = os.environ["OSMSG_PG_DSN"]
    safe_dsn = dsn.replace("'", "''")

    populated = populated_db_factory(fresh_db)
    populated.execute("INSTALL postgres")
    populated.execute("LOAD postgres")
    populated.execute(f"ATTACH '{safe_dsn}' AS pg_w (TYPE postgres)")
    try:
        for stmt in PG_SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                populated.execute(f"CALL postgres_execute('pg_w', $${stmt}$$)")
        for table in ("changeset_stats", "changesets", "users", "state"):
            populated.execute(f"CALL postgres_execute('pg_w', $$DELETE FROM {table}$$)")
    finally:
        populated.execute("DETACH pg_w")

    populated.execute(
        "INSERT INTO state VALUES ('https://planet.openstreetmap.org/replication/minute', 1, ?, ?)",
        [_dt.datetime(2026, 5, 1, tzinfo=_dt.UTC), _dt.datetime(2026, 5, 1, tzinfo=_dt.UTC)],
    )
    to_psql(populated, dsn)
    to_psql(populated, dsn)
