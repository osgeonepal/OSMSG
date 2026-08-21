"""Schema validation for the PostgreSQL exporter. A live push test needs a Postgres instance and is gated
behind `OSMSG_PG_DSN` (marked `network`, deselected by default).
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


def test_pg_schema_uses_native_tag_type():
    """Tags are stored as the native osmsg_tag composite array, not JSON, so the representation matches
    the DuckDB store and the rollup (one tag path everywhere queried)."""
    from api.pg_schema import PG_TAG_TYPE_SQL

    assert "tags           osmsg_tag[]" in PG_SCHEMA
    assert "JSONB" not in PG_SCHEMA
    assert "CREATE TYPE osmsg_tag AS (k text, v text, c bigint, m bigint, l double precision)" in PG_TAG_TYPE_SQL


def test_pg_schema_state_is_single_row_per_source():
    """`state` is keyed by source_url alone, one row per replication source, ever.
    The PSQL exporter UPSERTs on conflict so every osmsg run keeps PG in sync."""
    assert "source_url  TEXT PRIMARY KEY" in PG_SCHEMA
    assert "BIGSERIAL" not in PG_SCHEMA  # no synthetic ids needed


def test_superseded_changefile_sources_prunes_only_coarser_handoff_residue():
    """A day->minute handoff leaves the coarse day row in PG; the finer minute source the store now
    tracks supersedes it (disjoint by the boundary), so it is pruned. Unrelated sources are kept."""
    from osmsg.export.psql import _superseded_changefile_sources

    base = "https://planet.openstreetmap.org/replication"
    day, hour, minute = f"{base}/day", f"{base}/hour", f"{base}/minute"

    assert _superseded_changefile_sources({minute}, {day, hour, minute}) == {day, hour}
    assert _superseded_changefile_sources({hour}, {day, hour}) == {day}
    # No finer local source: nothing is superseded (no handoff happened).
    assert _superseded_changefile_sources({day}, {day, minute}) == set()
    # A non-changefile source (geofabrik country) is never treated as handoff residue.
    geofabrik = "https://download.geofabrik.de/asia/nepal-updates"
    assert _superseded_changefile_sources({minute}, {geofabrik, day}) == {day}


def test_pg_schema_statements_each_parse_with_postgres_extension():
    """Each individual CREATE statement is well-formed enough that the postgres
    extension's parser would accept it, we use DuckDB's own parser as an
    approximation (DuckDB's CREATE TABLE syntax is compatible)."""
    duckdb_clone = (
        PG_SCHEMA.replace("DOUBLE PRECISION", "DOUBLE")
        .replace("osmsg_tag[]", "STRUCT(k VARCHAR, v VARCHAR, c BIGINT, m BIGINT, l DOUBLE)[]")
        .replace("TEXT", "VARCHAR")
        .replace(" ON DELETE CASCADE", "")  # DuckDB's parser rejects the action clause; Postgres needs it
    )
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    for stmt in [s.strip() for s in duckdb_clone.split(";") if s.strip()]:
        upper = stmt.upper()
        if "CREATE INDEX" in upper and "USING" in upper:
            continue
        conn.execute(stmt)
    tables = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert {"users", "changesets", "changeset_stats", "state"} <= tables


def test_pg_schema_uses_explicit_postgres_index_types():
    assert "idx_changesets_created_at ON changesets USING BTREE (created_at)" in PG_SCHEMA
    assert "idx_changesets_hashtags ON changesets USING GIN (hashtags)" in PG_SCHEMA
    assert "idx_changesets_editor ON changesets USING BTREE (editor)" in PG_SCHEMA
    assert "idx_changesets_bbox ON changesets USING GIST" in PG_SCHEMA
    assert "box(point(min_lon, min_lat), point(max_lon, max_lat))" in PG_SCHEMA


EXPECTED_USER_STATS = {
    "alice": {"changesets": 1, "nodes_created": 30, "ways_created": 8, "poi_created": 5, "map_changes": 44},
    "bob": {"changesets": 1, "nodes_created": 50, "ways_created": 0, "poi_created": 50, "map_changes": 50},
}


def _assert_user_stats_match(actual: list[dict], expected: dict[str, dict[str, int]]) -> None:
    by_name = {r["name"]: r for r in actual}
    assert set(by_name) == set(expected), f"users mismatch: {set(by_name)} vs {set(expected)}"
    for name, fields in expected.items():
        for col, want in fields.items():
            assert by_name[name][col] == want, f"{name}.{col}: got {by_name[name][col]} want {want}"


def test_duckdb_user_stats_match_seed_data(fresh_db, populated_db_factory):
    """Anchor for EXPECTED_USER_STATS, if it drifts, every other roundtrip test silently lies."""
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
                   SUM(cs.nodes_created) AS nodes_created,
                   SUM(cs.ways_created)  AS ways_created,
                   SUM(cs.poi_created)   AS poi_created,
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

    cols = ("name", "changesets", "nodes_created", "ways_created", "poi_created", "map_changes")
    actual = [dict(zip(cols, r, strict=True)) for r in rows]
    _assert_user_stats_match(actual, EXPECTED_USER_STATS)


def test_merge_parquet_changeset_stats_native_tags(fresh_db, tmp_path):
    """Shards store `tags` as a native LIST<STRUCT>, so ingest is a direct copy (no JSON parse). Every
    row must land with its tags, including rows with no tags (empty list). Regression guard for the fix
    that dropped the JSON round-trip + nested json_each which OOMed on a large tag blob."""
    from osmsg.db.ingest import flush_rows_to_parquet, merge_parquet_files

    n = 10
    stats = [
        (
            1_000 + i,
            5000,
            99,
            i,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            [{"k": "building", "v": "yes", "c": i, "m": 0, "l": None}] if i % 2 == 0 else [],
        )
        for i in range(n)
    ]
    flush_rows_to_parquet(
        parquet_dir=tmp_path / "setbased",
        pid=1,
        batch_index=0,
        users=[(99, "lexoa")],
        changesets=[(1_000 + i, 99, None, None, None, None, None, None, None) for i in range(n)],
        changeset_stats=stats,
    )
    merge_parquet_files(fresh_db, tmp_path / "setbased", cleanup=True)

    count, total_nodes = fresh_db.execute(
        "SELECT COUNT(*), SUM(nodes_created) FROM changeset_stats WHERE changeset_id >= 1000 AND changeset_id < 1010"
    ).fetchone()
    assert count == n  # every row landed
    assert total_nodes == sum(range(n))
    tagged = fresh_db.execute("SELECT tags[1].c FROM changeset_stats WHERE changeset_id = 1006").fetchone()
    assert tagged == (6,)  # json->struct conversion correct
    untagged = fresh_db.execute("SELECT tags FROM changeset_stats WHERE changeset_id = 1007").fetchone()
    assert untagged == ([],)  # NULL tag_stats -> empty native list


def test_merge_parquet_upgrades_empty_changeset_when_richer_data_arrives(fresh_db, tmp_path):
    """Empty stub from tick 1 must be upgraded to richer data when tick 2 arrives."""
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
                [{"k": "building", "v": "yes", "c": 3, "m": 0, "l": None}],
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
            (
                900900,
                6000,
                77,
                5,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                5,
                0,
                [{"k": "shop", "v": "bakery", "c": 1, "m": 0, "l": None}],
            ),  # noqa: E501
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
    """A second push from the SAME source URL is fine, common --update path."""
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


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("OSMSG_PG_DSN"), reason="OSMSG_PG_DSN not set; live PG push not exercised")
def test_to_psql_bulk_load_rebuilds_indexes_and_keys(fresh_db, populated_db_factory):
    """bulk_load drops secondary indexes + FKs for the push, then rebuilds them; data and
    referential integrity must be intact afterwards."""
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

    to_psql(populated, dsn, bulk_load=True)

    check = duckdb.connect(":memory:")
    check.execute("INSTALL postgres")
    check.execute("LOAD postgres")
    check.execute(f"ATTACH '{safe_dsn}' AS pg (TYPE postgres, READ_ONLY)")
    try:
        stats = check.execute("SELECT count(*) FROM pg.changeset_stats").fetchone()[0]
        orphans = check.execute(
            "SELECT count(*) FROM pg.changeset_stats s "
            "LEFT JOIN pg.changesets c ON s.changeset_id = c.changeset_id WHERE c.changeset_id IS NULL"
        ).fetchone()[0]
        # Count foreign keys on the Postgres side (regclass is a Postgres type DuckDB cannot parse).
        fks = check.execute(
            "SELECT count(*) FROM postgres_query('pg', "
            "'SELECT 1 FROM information_schema.table_constraints "
            "WHERE constraint_type = ''FOREIGN KEY'' "
            "AND table_name IN (''changesets'', ''changeset_stats'')')"
        ).fetchone()[0]
    finally:
        check.execute("DETACH pg")
        check.close()

    assert stats > 0  # data loaded
    assert orphans == 0  # FK integrity holds after rebuild
    assert fks == 3  # the three foreign keys were recreated


class _BoundsStub:
    """Minimal DuckDB-connection stand-in: the only query _push_chunked runs is the count/min/max
    bounds probe, so return a fixed tuple for it and record nothing else."""

    def __init__(self, count, lo, hi):
        self._bounds = (count, lo, hi)

    def execute(self, sql):
        assert sql.startswith("SELECT count(*), min(changeset_id), max(changeset_id)")
        return self

    def fetchone(self):
        return self._bounds


def _count_pushes(count, lo, hi):
    from osmsg.export.psql import _push_chunked

    calls = []
    _push_chunked(_BoundsStub(count, lo, hi), "changeset_stats", lambda conn, where: calls.append(where))
    return calls


def test_push_chunked_small_delta_uses_single_statement():
    assert len(_count_pushes(302, 100, 400)) == 1


def test_push_chunked_scales_with_row_count_and_caps():
    from osmsg.export.psql import _BULK_COMMIT_CHUNKS, _CHUNK_TARGET_ROWS

    assert len(_count_pushes(2 * _CHUNK_TARGET_ROWS, 1, 10_000)) == 2
    assert len(_count_pushes(500 * _CHUNK_TARGET_ROWS, 1, 10**9)) == _BULK_COMMIT_CHUNKS


def test_push_chunked_empty_source_pushes_nothing():
    assert _count_pushes(0, None, None) == []


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("OSMSG_PG_DSN"), reason="OSMSG_PG_DSN not set; live PG push not exercised")
def test_metadata_only_changeset_fills_stub_without_fk_abort(fresh_db):
    """A long-open changeset's metadata arrives in a later, no-edit delta (its uid absent from this
    delta's changeset_stats). With history present the push takes the not_history branch, which must
    still carry that user, or the changesets FK aborts the whole push and the stub is stranded forever."""
    dsn = os.environ["OSMSG_PG_DSN"]
    safe_dsn = dsn.replace("'", "''")

    fresh_db.execute("INSTALL postgres")
    fresh_db.execute("LOAD postgres")
    fresh_db.execute(f"ATTACH '{safe_dsn}' AS pg_w (TYPE postgres)")
    try:
        for stmt in (s.strip() for s in PG_SCHEMA.strip().split(";")):
            if stmt:
                fresh_db.execute(f"CALL postgres_execute('pg_w', $${stmt}$$)")
        for table in ("changeset_hashtag", "changeset_stats", "changesets", "users", "state"):
            fresh_db.execute(f"CALL postgres_execute('pg_w', $$DELETE FROM {table}$$)")
        # A seq_id=0 history row makes _pg_has_history() true -> the not_history branch runs.
        fresh_db.execute("CALL postgres_execute('pg_w', $$INSERT INTO users VALUES (1, 'hist')$$)")
        fresh_db.execute("CALL postgres_execute('pg_w', $$INSERT INTO changesets (changeset_id, uid) VALUES (1, 1)$$)")
        fresh_db.execute(
            "CALL postgres_execute('pg_w', $$INSERT INTO changeset_stats "
            "(changeset_id, seq_id, uid, nodes_created, nodes_modified, nodes_deleted, ways_created, "
            "ways_modified, ways_deleted, rels_created, rels_modified, rels_deleted, poi_created, poi_modified) "
            "VALUES (1, 0, 1, 0,0,0,0,0,0,0,0,0,0,0)$$)"
        )
    finally:
        fresh_db.execute("DETACH pg_w")

    # Delta buffer: changeset 500 is metadata-only (uid 100, full metadata, NO stats this tick);
    # changeset 600 has live edits (uid 200). uid 100 is absent from this delta's changeset_stats.
    fresh_db.execute("INSERT INTO users VALUES (100, 'stubuser'), (200, 'liveuser')")
    fresh_db.execute(
        "INSERT INTO changesets VALUES "
        "(500, 100, '2026-08-04 02:01:51+00', ['#msf'], 'iD', NULL), "
        "(600, 200, '2026-08-04 03:00:00+00', ['#msf'], 'iD', NULL)"
    )
    fresh_db.execute("INSERT INTO changeset_stats VALUES (600, 7228931, 200, 10,0,0,2,0,0,0,0,0,0,0, NULL)")

    to_psql(fresh_db, dsn)  # must not raise the changesets_uid_fkey violation

    verifier = duckdb.connect(":memory:")
    verifier.execute("INSTALL postgres")
    verifier.execute("LOAD postgres")
    verifier.execute(f"ATTACH '{safe_dsn}' AS pg_r (TYPE postgres, READ_ONLY)")
    try:
        landed = verifier.execute("SELECT created_at FROM pg_r.changesets WHERE changeset_id = 500").fetchone()
        user_ok = verifier.execute("SELECT count(*) FROM pg_r.users WHERE uid = 100").fetchone()[0]
        hashtag_ok = verifier.execute(
            "SELECT count(*) FROM pg_r.changeset_hashtag WHERE changeset_id = 500 AND hashtag = '#msf'"
        ).fetchone()[0]
    finally:
        verifier.execute("DETACH pg_r")
        verifier.close()

    assert landed is not None and landed[0] is not None  # metadata-only changeset reached PG
    assert user_ok == 1  # its user was carried even without an edit row this tick
    assert hashtag_ok == 1  # and its #msf hashtag reached the index (the stub is no longer stranded)
