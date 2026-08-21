"""Prune Postgres of rows now served from published history, keeping it to the uncovered live tail."""

import datetime as dt

import duckdb

from .exceptions import OsmsgError
from .history import fetch_manifest
from .ui import info

DEFAULT_OVERLAP = dt.timedelta(days=2)


def _attach(dsn: str) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect()
    conn.execute("INSTALL postgres")
    conn.execute("LOAD postgres")
    conn.execute(f"ATTACH '{dsn.replace(chr(39), chr(39) * 2)}' AS pg (TYPE postgres)")
    return conn


def _pg_execute(conn: duckdb.DuckDBPyConnection, sql: str) -> None:
    """Run one statement natively on the attached Postgres, so a bulk DELETE is a single indexed
    statement server-side instead of DuckDB's per-row ctid batches."""
    conn.execute(f"CALL postgres_execute('pg', $osmsg_stmt${sql}$osmsg_stmt$)")


def prune_pg(dsn: str, cutoff: dt.datetime) -> tuple[int, int]:
    """Delete changesets and their changeset_stats older than cutoff; child rows first for the FK. The
    DSN is interpolated into ATTACH, so it must be trusted. Counting and deleting use separate
    connections because a read pins the connection read-only, which would block the native deletes."""
    iso = cutoff.astimezone(dt.UTC).isoformat()
    older = f"created_at < TIMESTAMPTZ '{iso}'"

    reader = _attach(dsn)
    stats_row = reader.execute(
        "SELECT count(*) FROM pg.changeset_stats s "
        f"WHERE EXISTS (SELECT 1 FROM pg.changesets c WHERE c.changeset_id = s.changeset_id AND c.{older})"
    ).fetchone()
    cs_row = reader.execute(f"SELECT count(*) FROM pg.changesets WHERE {older}").fetchone()
    reader.close()
    stats_n = stats_row[0] if stats_row else 0
    cs_n = cs_row[0] if cs_row else 0

    if cs_n:
        writer = _attach(dsn)
        _pg_execute(
            writer,
            f"DELETE FROM changeset_stats s USING changesets c WHERE s.changeset_id = c.changeset_id AND c.{older}",
        )
        _pg_execute(writer, f"DELETE FROM changesets WHERE {older}")
        writer.close()
    return stats_n, cs_n


def prune_covered(dsn: str, history_url: str, overlap: dt.timedelta = DEFAULT_OVERLAP) -> tuple[int, int]:
    """Prune Postgres up to the published frontier minus the overlap buffer."""
    manifest = fetch_manifest(history_url)
    if manifest is None:
        raise OsmsgError(f"could not read the manifest at {history_url}")
    cutoff = manifest.frontier - overlap
    stats_n, cs_n = prune_pg(dsn, cutoff)
    info(f"prune: cutoff {cutoff.astimezone(dt.UTC).isoformat()} -> deleted {cs_n:,} changesets, {stats_n:,} stats")
    return stats_n, cs_n
