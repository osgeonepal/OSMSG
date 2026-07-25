"""Per-worker parquet writers + bulk merge into DuckDB."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

# Native shard column for the per-changeset tag breakdown, matching the store's
# STRUCT(k VARCHAR, v VARCHAR, c BIGINT, m BIGINT, len_m DOUBLE)[] so ingest is a direct copy.
_TAG_PA_TYPE = pa.list_(
    pa.struct(
        [
            pa.field("k", pa.string()),
            pa.field("v", pa.string()),
            pa.field("c", pa.int64()),
            pa.field("m", pa.int64()),
            pa.field("len_m", pa.float64()),
        ]
    )
)


def _quarantine_corrupt(parquet_dir: Path) -> None:
    """Rename unreadable parquet shards out of the way so the bulk read doesn't abort."""
    for shard in parquet_dir.glob("temp_*.parquet"):
        try:
            pq.read_metadata(shard)
        except Exception as exc:  # noqa: BLE001
            corrupt = shard.with_suffix(".corrupt")
            shard.rename(corrupt)
            print(f"warning: skipping unreadable shard {shard.name}: {exc}", file=sys.stderr)


USERS_SCHEMA = pa.schema(
    [
        pa.field("uid", pa.int64(), nullable=False),
        pa.field("username", pa.string(), nullable=False),
    ]
)

CHANGESETS_SCHEMA = pa.schema(
    [
        pa.field("changeset_id", pa.int64(), nullable=False),
        pa.field("uid", pa.int64(), nullable=False),
        pa.field("created_at", pa.timestamp("s", tz="UTC")),
        pa.field("hashtags", pa.list_(pa.string())),
        pa.field("editor", pa.string()),
        pa.field("min_lon", pa.float64()),
        pa.field("min_lat", pa.float64()),
        pa.field("max_lon", pa.float64()),
        pa.field("max_lat", pa.float64()),
    ]
)

CHANGESET_STATS_SCHEMA = pa.schema(
    [
        pa.field("changeset_id", pa.int64(), nullable=False),
        pa.field("seq_id", pa.int64(), nullable=False),
        pa.field("uid", pa.int64(), nullable=False),
        pa.field("nodes_created", pa.int32()),
        pa.field("nodes_modified", pa.int32()),
        pa.field("nodes_deleted", pa.int32()),
        pa.field("ways_created", pa.int32()),
        pa.field("ways_modified", pa.int32()),
        pa.field("ways_deleted", pa.int32()),
        pa.field("rels_created", pa.int32()),
        pa.field("rels_modified", pa.int32()),
        pa.field("rels_deleted", pa.int32()),
        pa.field("poi_created", pa.int32()),
        pa.field("poi_modified", pa.int32()),
        pa.field("tags", _TAG_PA_TYPE),
    ]
)


def _write(rows: list[tuple], schema: pa.Schema, path: Path) -> Path | None:
    if not rows:
        return None
    columns = list(zip(*rows, strict=True))
    arrays = [pa.array(col, type=field.type) for col, field in zip(columns, schema, strict=True)]
    table = pa.table(dict(zip(schema.names, arrays, strict=True)))
    pq.write_table(table, path, compression="snappy")
    return path


def flush_rows_to_parquet(
    *,
    parquet_dir: Path,
    pid: int,
    batch_index: int,
    users: list[tuple],
    changesets: list[tuple],
    changeset_stats: list[tuple] | None = None,
) -> dict[str, Path | None]:
    parquet_dir.mkdir(parents=True, exist_ok=True)
    fmt = f"temp_{pid}_{{name}}_{batch_index}.parquet"
    return {
        "users": _write(users, USERS_SCHEMA, parquet_dir / fmt.format(name="users")),
        "changesets": _write(changesets, CHANGESETS_SCHEMA, parquet_dir / fmt.format(name="changesets")),
        "changeset_stats": _write(
            changeset_stats or [], CHANGESET_STATS_SCHEMA, parquet_dir / fmt.format(name="changeset_stats")
        ),
    }


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def merge_parquet_files(conn: duckdb.DuckDBPyConnection, parquet_dir: Path, *, cleanup: bool = True) -> None:
    parquet_dir = Path(parquet_dir)
    if not parquet_dir.exists():
        return

    _quarantine_corrupt(parquet_dir)

    def pattern(name: str) -> str:
        # read_parquet() takes a literal, escape so quoted paths can't break out.
        return _sql_escape((parquet_dir / f"temp_*_{name}_*.parquet").as_posix())

    # No enclosing transaction: one transaction over a large window pins too much in memory. Each
    # statement is idempotent (INSERT OR IGNORE / COALESCE), so per-step auto-commit is crash-safe.
    conn.execute("SET preserve_insertion_order = false")  # stream, don't buffer to preserve order
    try:
        if any(parquet_dir.glob("temp_*_users_*.parquet")):
            conn.execute(f"INSERT OR IGNORE INTO users SELECT uid, username FROM read_parquet('{pattern('users')}')")
        if any(parquet_dir.glob("temp_*_changesets_*.parquet")):
            conn.execute("INSTALL spatial")
            conn.execute("LOAD spatial")
            conn.execute(
                f"""
                INSERT OR IGNORE INTO changesets
                SELECT changeset_id, uid, created_at, hashtags, editor,
                       CASE WHEN min_lon IS NOT NULL
                           THEN ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat)
                       END
                FROM read_parquet('{pattern("changesets")}')
                """
            )
            # Newer non-NULL wins; dedupe src so multiple emits per window don't trip the PK on UPDATE.
            conn.execute(
                f"""
                UPDATE changesets c
                SET created_at = COALESCE(src.created_at, c.created_at),
                    hashtags   = COALESCE(src.hashtags,   c.hashtags),
                    editor     = COALESCE(src.editor,     c.editor),
                    geom       = COALESCE(src.geom,       c.geom)
                FROM (
                    SELECT DISTINCT ON (changeset_id)
                           changeset_id, created_at, hashtags, editor,
                           CASE WHEN min_lon IS NOT NULL
                               THEN ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat)
                           END AS geom
                    FROM read_parquet('{pattern("changesets")}')
                    ORDER BY changeset_id,
                             (min_lon IS NOT NULL) DESC,
                             (editor IS NOT NULL)  DESC,
                             (hashtags IS NOT NULL) DESC,
                             created_at DESC NULLS LAST
                ) src
                WHERE c.changeset_id = src.changeset_id
                  AND (src.created_at IS NOT NULL OR src.hashtags IS NOT NULL
                       OR src.editor IS NOT NULL OR src.geom IS NOT NULL)
                """
            )
        if any(parquet_dir.glob("temp_*_changeset_stats_*.parquet")):
            # The shard stores `tags` as a native LIST<STRUCT> (built in the handler), so ingest is a
            # direct column copy.
            conn.execute(
                f"""
                INSERT OR IGNORE INTO changeset_stats
                SELECT changeset_id, seq_id, uid,
                       nodes_created, nodes_modified, nodes_deleted,
                       ways_created,  ways_modified,  ways_deleted,
                       rels_created,  rels_modified,  rels_deleted,
                       poi_created,   poi_modified,
                       tags
                FROM read_parquet('{pattern("changeset_stats")}')
                """
            )
    finally:
        conn.execute("SET preserve_insertion_order = true")

    if cleanup:
        shutil.rmtree(parquet_dir, ignore_errors=True)
