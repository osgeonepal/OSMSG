"""Export rows as a single Parquet file (queryable via DuckDB / polars / pandas)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


def _coerce(value: Any) -> Any:
    if isinstance(value, dict):
        return json.dumps(value)
    return value


def to_parquet(rows: list[dict[str, Any]], output_path: Path) -> Path:
    """Write rows to Parquet, JSON-encoding nested dicts (e.g. tags_create/tags_modify)."""
    if not rows:
        raise ValueError("nothing to export")
    columns = sorted({k for r in rows for k in r}, key=_priority_key)
    data = {c: [_coerce(r.get(c)) for r in rows] for c in columns}
    table = pa.table(data)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, output_path, compression="snappy")
    return output_path


_PRIORITY = (
    "rank",
    "uid",
    "name",
    "date",
    "changesets",
    "users",
    "map_changes",
    "nodes_created",
    "nodes_modified",
    "nodes_deleted",
    "ways_created",
    "ways_modified",
    "ways_deleted",
    "rels_created",
    "rels_modified",
    "rels_deleted",
    "poi_created",
    "poi_modified",
    "hashtags",
    "editors",
    "tags_create",
    "tags_modify",
    "start_date",
    "end_date",
)


def _priority_key(name: str) -> tuple[int, str]:
    try:
        return (_PRIORITY.index(name), name)
    except ValueError:
        return (len(_PRIORITY), name)
