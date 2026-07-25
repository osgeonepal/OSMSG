"""Prune Postgres of rows now served from published history, keeping it to the uncovered live tail."""

import datetime as dt

import duckdb

from .exceptions import OsmsgError
from .history import fetch_manifest
from .ui import info

DEFAULT_OVERLAP = dt.timedelta(days=2)


def prune_pg(dsn: str, cutoff: dt.datetime) -> tuple[int, int]:
    """Delete changesets and their changeset_stats older than cutoff; child rows first for the FK. The
    DSN is interpolated into ATTACH, so it must be trusted."""
    conn = duckdb.connect()
    conn.execute("INSTALL postgres")
    conn.execute("LOAD postgres")
    conn.execute(f"ATTACH '{dsn.replace(chr(39), chr(39) * 2)}' AS pg (TYPE postgres)")
    iso = cutoff.astimezone(dt.UTC).isoformat()
    old_cs = f"SELECT changeset_id FROM pg.changesets WHERE created_at < TIMESTAMPTZ '{iso}'"
    stats_row = conn.execute(f"SELECT count(*) FROM pg.changeset_stats WHERE changeset_id IN ({old_cs})").fetchone()
    cs_row = conn.execute(f"SELECT count(*) FROM pg.changesets WHERE created_at < TIMESTAMPTZ '{iso}'").fetchone()
    stats_n = stats_row[0] if stats_row else 0
    cs_n = cs_row[0] if cs_row else 0
    if cs_n:
        conn.execute(f"DELETE FROM pg.changeset_stats WHERE changeset_id IN ({old_cs})")
        conn.execute(f"DELETE FROM pg.changesets WHERE created_at < TIMESTAMPTZ '{iso}'")
    conn.close()
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
