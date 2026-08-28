"""Backfill 'stub' changesets (edits captured but metadata missing) from the OSM API. Still-open
changesets are left to fill on close."""

import json
import re
from urllib.request import urlopen

import duckdb

from ..ui import info

_HASHTAG_RE = re.compile(r"#[\w-]+")
_API = "https://api.openstreetmap.org/api/0.6/changesets.json?changesets="
_BATCH = 100


def _attach(dsn: str) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect()
    conn.execute("INSTALL postgres")
    conn.execute("LOAD postgres")
    conn.execute(f"ATTACH '{dsn.replace(chr(39), chr(39) * 2)}' AS pg (TYPE postgres)")
    return conn


def _pg(conn: duckdb.DuckDBPyConnection, sql: str) -> None:
    conn.execute(f"CALL postgres_execute('pg', $osmsg_stmt${sql}$osmsg_stmt$)")


def stub_ids(conn: duckdb.DuckDBPyConnection) -> list[int]:
    rows = conn.execute(
        "SELECT c.changeset_id FROM pg.changesets c WHERE c.created_at IS NULL "
        "AND EXISTS (SELECT 1 FROM pg.changeset_stats s WHERE s.changeset_id = c.changeset_id)"
    ).fetchall()
    return [int(r[0]) for r in rows]


def fetch_closed(ids: list[int]) -> dict[int, dict]:
    """OSM-API metadata for the given changesets, in batches; only closed ones (open ones aren't final)."""
    out: dict[int, dict] = {}
    for i in range(0, len(ids), _BATCH):
        chunk = ids[i : i + _BATCH]
        with urlopen(_API + ",".join(str(x) for x in chunk), timeout=60) as response:
            payload = json.load(response)
        for cs in payload.get("changesets", []):
            if cs.get("open"):
                continue
            tags = cs.get("tags", {})
            haystack = tags.get("comment", "") + "\n" + tags.get("hashtags", "")
            out[int(cs["id"])] = {
                "created_at": cs["created_at"],
                "editor": tags.get("created_by"),
                "hashtags": sorted(set(_HASHTAG_RE.findall(haystack))),
                "min_lon": cs.get("min_lon"),
                "min_lat": cs.get("min_lat"),
                "max_lon": cs.get("max_lon"),
                "max_lat": cs.get("max_lat"),
            }
    return out


# Postgres string-literal escaping: the sanitize boundary for OSM tag values (editor, comment) built into SQL.
def _lit(value: object) -> str:
    return "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"


def _num(value: float | None) -> str:
    return "NULL" if value is None else repr(float(value))


def _arr(values: list[str]) -> str:
    return "ARRAY[" + ",".join(_lit(v) for v in values) + "]::text[]" if values else "ARRAY[]::text[]"


def _apply(conn: duckdb.DuckDBPyConnection, meta: dict[int, dict]) -> None:
    items = list(meta.items())
    for i in range(0, len(items), _BATCH):
        rows = ", ".join(
            f"({cid}, TIMESTAMPTZ {_lit(m['created_at'])}, {_lit(m['editor'])}, {_arr(m['hashtags'])}, "
            f"{_num(m['min_lon'])}, {_num(m['min_lat'])}, {_num(m['max_lon'])}, {_num(m['max_lat'])})"
            for cid, m in items[i : i + _BATCH]
        )
        cte = (
            f"WITH v(changeset_id, created_at, editor, hashtags, min_lon, min_lat, max_lon, max_lat) AS (VALUES {rows})"
        )
        _pg(
            conn,
            f"{cte} UPDATE changesets c SET created_at = v.created_at, editor = COALESCE(c.editor, v.editor), "
            "hashtags = v.hashtags, min_lon = v.min_lon, min_lat = v.min_lat, max_lon = v.max_lon, "
            "max_lat = v.max_lat FROM v WHERE c.changeset_id = v.changeset_id AND c.created_at IS NULL",
        )
        _pg(
            conn,
            f"{cte} INSERT INTO changeset_hashtag (hashtag, changeset_id, created_at) "
            "SELECT lower(h), v.changeset_id, v.created_at FROM v, unnest(v.hashtags) AS h ON CONFLICT DO NOTHING",
        )


def check_stubs(dsn: str, fix: bool = False) -> tuple[int, int]:
    """Report, and with fix=True repair, stub changesets. Returns (stubs_found, repaired)."""
    conn = _attach(dsn)
    try:
        ids = stub_ids(conn)
        if not ids:
            info("check: no stub changesets.")
            return (0, 0)
        info(f"check: {len(ids):,} stub changesets missing metadata.")
        if not fix:
            return (len(ids), 0)
        meta = fetch_closed(ids)
        if meta:
            _apply(conn, meta)
        info(f"check: repaired {len(meta):,}; {len(ids) - len(meta):,} still open, will heal on close.")
        return (len(ids), len(meta))
    finally:
        conn.close()
