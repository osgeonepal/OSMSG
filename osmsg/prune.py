"""Prune Postgres of the recent tail now covered by published history. When a month is published to
HuggingFace the frontier advances; rows older than (frontier - overlap) are then served from the
published history and never read from Postgres, so they can be deleted to keep it to the live tail.
The overlap buffer keeps a couple of days beyond the frontier as belt-and-suspenders for a month that
stops slightly short of its boundary."""

import datetime as dt

import duckdb

from .exceptions import OsmsgError
from .history import fetch_manifest
from .ui import info

DEFAULT_OVERLAP = dt.timedelta(days=2)


def prune_pg(dsn: str, cutoff: dt.datetime) -> tuple[int, int]:
    """Delete changesets (and their changeset_stats) with created_at < cutoff from the Postgres target.
    Child rows go first to respect the FK. Returns (changeset_stats_deleted, changesets_deleted). The DSN
    is interpolated into ATTACH, so it must be trusted."""
    conn = duckdb.connect()
    conn.execute("INSTALL postgres")
    conn.execute("LOAD postgres")
    conn.execute(f"ATTACH '{dsn.replace(chr(39), chr(39) * 2)}' AS pg (TYPE postgres)")
    iso = cutoff.astimezone(dt.UTC).isoformat()
    old_cs = f"SELECT changeset_id FROM pg.changesets WHERE created_at < TIMESTAMPTZ '{iso}'"
    stats_row = conn.execute(
        f"SELECT count(*) FROM pg.changeset_stats WHERE changeset_id IN ({old_cs})"
    ).fetchone()
    cs_row = conn.execute(f"SELECT count(*) FROM pg.changesets WHERE created_at < TIMESTAMPTZ '{iso}'").fetchone()
    stats_n = stats_row[0] if stats_row else 0
    cs_n = cs_row[0] if cs_row else 0
    if cs_n:
        conn.execute(f"DELETE FROM pg.changeset_stats WHERE changeset_id IN ({old_cs})")
        conn.execute(f"DELETE FROM pg.changesets WHERE created_at < TIMESTAMPTZ '{iso}'")
    conn.close()
    return stats_n, cs_n


def prune_covered(dsn: str, history_url: str, overlap: dt.timedelta = DEFAULT_OVERLAP) -> tuple[int, int]:
    """Read the published frontier and prune Postgres up to (frontier - overlap). Returns the deleted
    (changeset_stats, changesets) counts; (0, 0) when the manifest is unreadable or nothing is covered."""
    manifest = fetch_manifest(history_url)
    if manifest is None:
        raise OsmsgError(f"could not read the manifest at {history_url}")
    cutoff = manifest.frontier - overlap
    stats_n, cs_n = prune_pg(dsn, cutoff)
    info(f"prune: cutoff {cutoff.astimezone(dt.UTC).isoformat()} -> deleted {cs_n:,} changesets, {stats_n:,} stats")
    return stats_n, cs_n
