"""PostgreSQL exporter via DuckDB's postgres extension."""

import duckdb

from ..exceptions import OsmsgError
from ..pg_schema import PG_SCHEMA, PG_TAG_TYPE_SQL

_BULK_INDEXES = [
    ("idx_changesets_created_at", "CREATE INDEX idx_changesets_created_at ON changesets USING BTREE (created_at)"),
    (
        "idx_changesets_bbox",
        "CREATE INDEX idx_changesets_bbox ON changesets USING GIST "
        "(box(point(min_lon, min_lat), point(max_lon, max_lat)))",
    ),
    ("idx_changeset_stats_uid", "CREATE INDEX idx_changeset_stats_uid ON changeset_stats USING BTREE (uid)"),
]
_BULK_FKS = [
    ("changesets", "changesets_uid_fkey", "FOREIGN KEY (uid) REFERENCES users (uid)"),
    (
        "changeset_stats",
        "changeset_stats_changeset_id_fkey",
        "FOREIGN KEY (changeset_id) REFERENCES changesets (changeset_id)",
    ),
    ("changeset_stats", "changeset_stats_uid_fkey", "FOREIGN KEY (uid) REFERENCES users (uid)"),
]


def _changeset_bbox_select(conn: duckdb.DuckDBPyConnection) -> str:
    # Scope to the local store: after ATTACH, the pg_target.changesets columns (min_lon..) also match
    # table_name='changesets' and would otherwise mask the local geom column.
    columns = {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'changesets' AND table_catalog = current_database()"
        ).fetchall()
    }
    if {"min_lon", "min_lat", "max_lon", "max_lat"}.issubset(columns):
        return "min_lon, min_lat, max_lon, max_lat"
    if "geom" in columns:
        return """
                CASE WHEN geom IS NOT NULL THEN ST_XMin(geom) END AS min_lon,
                CASE WHEN geom IS NOT NULL THEN ST_YMin(geom) END AS min_lat,
                CASE WHEN geom IS NOT NULL THEN ST_XMax(geom) END AS max_lon,
                CASE WHEN geom IS NOT NULL THEN ST_YMax(geom) END AS max_lat
        """
    return """
                NULL::DOUBLE AS min_lon,
                NULL::DOUBLE AS min_lat,
                NULL::DOUBLE AS max_lon,
                NULL::DOUBLE AS max_lat
    """


_BULK_COMMIT_CHUNKS = 32


def _pg(conn: duckdb.DuckDBPyConnection, sql: str) -> None:
    # A named dollar tag for the outer literal so a statement carrying its own $$ / $tag$ (the guarded
    # CREATE TYPE) does not terminate the wrapper early.
    conn.execute(f"CALL postgres_execute('pg_target', $osmsg_stmt${sql}$osmsg_stmt$)")


def _pg_has_history(conn: duckdb.DuckDBPyConnection) -> bool:
    """True if the PG target already holds the history layer (seq_id=0); checked cheaply with LIMIT 1."""
    probe = "SELECT count(*) FROM (SELECT 1 FROM pg_target.changeset_stats WHERE seq_id = 0 LIMIT 1) t"
    row = conn.execute(probe).fetchone()
    return bool(row and row[0])


def _push_changesets(conn: duckdb.DuckDBPyConnection, where: str = "") -> None:
    # Newer non-NULL wins, NULL never downgrades (mirrors the DuckDB-side merge).
    bbox_select = _changeset_bbox_select(conn)
    conn.execute(
        f"""
        INSERT INTO pg_target.changesets AS c (
            changeset_id, uid, created_at, hashtags, editor,
            min_lon, min_lat, max_lon, max_lat
        )
        SELECT
            changeset_id,
            uid,
            created_at,
            hashtags,
            editor,
            {bbox_select}
        FROM changesets {where}
        ON CONFLICT (changeset_id) DO UPDATE SET
            created_at = COALESCE(EXCLUDED.created_at, c.created_at),
            hashtags   = COALESCE(EXCLUDED.hashtags,   c.hashtags),
            editor     = COALESCE(EXCLUDED.editor,     c.editor),
            min_lon    = COALESCE(EXCLUDED.min_lon,    c.min_lon),
            min_lat    = COALESCE(EXCLUDED.min_lat,    c.min_lat),
            max_lon    = COALESCE(EXCLUDED.max_lon,    c.max_lon),
            max_lat    = COALESCE(EXCLUDED.max_lat,    c.max_lat)
        """
    )


def _push_changeset_stats(conn: duckdb.DuckDBPyConnection, where: str = "") -> None:
    conn.execute(f"INSERT INTO pg_target.changeset_stats SELECT * FROM changeset_stats {where} ON CONFLICT DO NOTHING")


def _push_chunked(conn: duckdb.DuckDBPyConnection, source: str, push) -> None:
    """Call push() once per changeset_id range so each range commits on its own."""
    bounds = conn.execute(f"SELECT min(changeset_id), max(changeset_id) FROM {source}").fetchone()
    if not bounds or bounds[0] is None:
        return
    lo, hi = bounds
    step = (hi - lo) // _BULK_COMMIT_CHUNKS + 1
    cursor = lo
    while cursor <= hi:
        push(conn, f"WHERE changeset_id >= {cursor} AND changeset_id < {cursor + step}")
        cursor += step


_CHANGEFILE_RANK = {"/day": 1, "/hour": 2, "/minute": 3}


def _changefile_rank(source_url: str) -> int:
    """Coarse->fine rank of a planet changefile replication source (day<hour<minute); 0 if the URL is
    not a changefile granularity (e.g. the changesets stream or a geofabrik country source)."""
    for suffix, rank in _CHANGEFILE_RANK.items():
        if source_url.endswith(suffix):
            return rank
    return 0


def _superseded_changefile_sources(local_sources: set[str], existing_sources: set[str]) -> set[str]:
    """PG changefile sources coarser than the finest changefile source the local store now tracks. These
    are the residue of a clean day->hour->minute handoff (disjoint coverage), safe to retire in PG."""
    local_finest = max((_changefile_rank(u) for u in local_sources), default=0)
    if not local_finest:
        return set()
    return {u for u in existing_sources if 0 < _changefile_rank(u) < local_finest}


def to_psql(conn: duckdb.DuckDBPyConnection, dsn: str, *, bulk_load: bool = False) -> None:
    """Push every osmsg table to the libpq DSN target. bulk_load is for the one-time full-history
    import (drops indexes and foreign keys, streams, rebuilds, commits per range); leave it off for
    incremental --update pushes. The DSN is interpolated into ATTACH, so it must be trusted."""
    conn.execute("INSTALL postgres")
    conn.execute("LOAD postgres")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    safe_dsn = dsn.replace("'", "''")
    conn.execute(f"ATTACH '{safe_dsn}' AS pg_target (TYPE postgres)")
    try:
        # The changeset_stats.tags column depends on this composite type. Create it as the first PG op
        # (a fresh write transaction; a prior read would pin the connection read-only). On re-push the
        # type already exists, which is the expected idempotent case; anything else is a real failure.
        try:
            _pg(conn, PG_TAG_TYPE_SQL)
        except duckdb.Error as exc:
            if "already exists" not in str(exc):
                raise
        for stmt in PG_SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                _pg(conn, stmt)

        # Refuse cross-source push: would double-count via the (seq_id, changeset_id) PK.
        local_sources = {r[0] for r in conn.execute("SELECT source_url FROM state").fetchall()}
        existing_sources = {r[0] for r in conn.execute("SELECT source_url FROM pg_target.state").fetchall()}
        # A day->hour->minute handoff leaves the coarse source's stale resume row in PG; prune it (data is
        # time-disjoint by the boundary) so the mixing guard below does not trip on it. Only the row goes.
        superseded = _superseded_changefile_sources(local_sources, existing_sources)
        for stale in superseded:
            conn.execute("DELETE FROM pg_target.state WHERE source_url = ?", [stale])
        existing_sources -= superseded
        # All pushes are ON CONFLICT DO NOTHING, so order is irrelevant; stream instead of buffering.
        conn.execute("SET preserve_insertion_order = false")
        cross_source = existing_sources - local_sources
        if cross_source and local_sources:
            raise OsmsgError(
                f"PG target already has data from source(s) {sorted(cross_source)} "
                f"but this run pushes from {sorted(local_sources)}. Mixing sources "
                f"double-counts via the (seq_id, changeset_id) key. Use a separate "
                f"--psql-dsn, or wipe the existing PG tables first."
            )

        if bulk_load:
            for table, name, _add in _BULK_FKS:
                _pg(conn, f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
            for name, _create in _BULK_INDEXES:
                _pg(conn, f"DROP INDEX IF EXISTS {name}")
            conn.execute("INSERT INTO pg_target.users SELECT * FROM users ON CONFLICT DO NOTHING")
            _push_chunked(conn, "changesets", _push_changesets)
            _push_chunked(conn, "changeset_stats", _push_changeset_stats)
        elif _pg_has_history(conn):
            live_ids = "changeset_id IN (SELECT changeset_id FROM changeset_stats WHERE seq_id <> 0)"
            conn.execute(
                "INSERT INTO pg_target.users SELECT * FROM users "
                "WHERE uid IN (SELECT uid FROM changeset_stats WHERE seq_id <> 0) ON CONFLICT DO NOTHING"
            )
            _push_changesets(conn, f"WHERE {live_ids}")
            _push_changeset_stats(conn, "WHERE seq_id <> 0")
        else:
            conn.execute("INSERT INTO pg_target.users SELECT * FROM users ON CONFLICT DO NOTHING")
            _push_changesets(conn)
            _push_changeset_stats(conn)

        conn.execute(
            """
            INSERT INTO pg_target.state (source_url, last_seq, last_ts, updated_at)
            SELECT source_url, last_seq, last_ts, updated_at FROM state
            ON CONFLICT (source_url) DO UPDATE SET
                last_seq   = EXCLUDED.last_seq,
                last_ts    = EXCLUDED.last_ts,
                updated_at = EXCLUDED.updated_at
            """
        )

        if bulk_load:
            for table, name, add in _BULK_FKS:
                _pg(conn, f"ALTER TABLE {table} ADD CONSTRAINT {name} {add}")
            for _name, create in _BULK_INDEXES:
                _pg(conn, f"SET maintenance_work_mem = '512MB'; {create}")
            _pg(conn, "ANALYZE users")
            _pg(conn, "ANALYZE changesets")
            _pg(conn, "ANALYZE changeset_stats")
    finally:
        conn.execute("DETACH pg_target")


__all__ = ["PG_SCHEMA", "to_psql"]
