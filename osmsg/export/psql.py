"""PostgreSQL exporter via DuckDB's postgres extension."""

import duckdb

from ..exceptions import OsmsgError
from ..pg_schema import PG_SCHEMA


def _changeset_bbox_select(conn: duckdb.DuckDBPyConnection) -> str:
    columns = {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'changesets'"
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


def to_psql(conn: duckdb.DuckDBPyConnection, dsn: str) -> None:
    """Push every osmsg table to the libpq DSN target.

    DSN must be trusted — it is interpolated directly into the ATTACH statement.
    """
    conn.execute("INSTALL postgres")
    conn.execute("LOAD postgres")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    safe_dsn = dsn.replace("'", "''")
    conn.execute(f"ATTACH '{safe_dsn}' AS pg_target (TYPE postgres)")
    try:
        for stmt in PG_SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(f"CALL postgres_execute('pg_target', $${stmt}$$)")

        # Refuse cross-source push: would double-count via the (seq_id, changeset_id) PK.
        local_sources = {r[0] for r in conn.execute("SELECT source_url FROM state").fetchall()}
        existing_sources = {r[0] for r in conn.execute("SELECT source_url FROM pg_target.state").fetchall()}
        cross_source = existing_sources - local_sources
        if cross_source and local_sources:
            raise OsmsgError(
                f"PG target already has data from source(s) {sorted(cross_source)} "
                f"but this run pushes from {sorted(local_sources)}. Mixing sources "
                f"double-counts via the (seq_id, changeset_id) key. Use a separate "
                f"--psql-dsn, or wipe the existing PG tables first."
            )

        conn.execute("INSERT INTO pg_target.users SELECT * FROM users ON CONFLICT DO NOTHING")

        bbox_select = _changeset_bbox_select(conn)

        # Mirrors the DuckDB-side merge: newer non-NULL wins, NULL never downgrades.
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
            FROM changesets
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

        conn.execute("INSERT INTO pg_target.changeset_stats SELECT * FROM changeset_stats ON CONFLICT DO NOTHING")

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
    finally:
        conn.execute("DETACH pg_target")


__all__ = ["PG_SCHEMA", "to_psql"]
