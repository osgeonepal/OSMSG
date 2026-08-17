from __future__ import annotations

import os
import re
from typing import Any

import duckdb

from .duckdb_schema import DUCKDB_SCHEMA

_MEMORY_LIMIT_RE = re.compile(r"^\d+(\.\d+)?\s?(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$", re.IGNORECASE)


def _apply_runtime_pragmas(conn: duckdb.DuckDBPyConnection) -> None:
    """Bound DuckDB memory and point spilling at a roomy disk, from operator env. Unset means DuckDB
    defaults, so a large merge spills to disk instead of OOMing on a memory-capped host."""
    memory_limit = os.environ.get("OSMSG_DUCKDB_MEMORY_LIMIT")
    if memory_limit:
        if not _MEMORY_LIMIT_RE.match(memory_limit):
            raise ValueError(f"OSMSG_DUCKDB_MEMORY_LIMIT must be like '1GB', got {memory_limit!r}")
        conn.execute(f"SET memory_limit='{memory_limit}'")
    threads = os.environ.get("OSMSG_DUCKDB_THREADS")
    if threads:
        conn.execute(f"SET threads={int(threads)}")
    temp_directory = os.environ.get("OSMSG_DUCKDB_TEMP_DIR")
    if temp_directory:
        os.makedirs(temp_directory, exist_ok=True)
        conn.execute(f"SET temp_directory='{temp_directory.replace(chr(39), chr(39) * 2)}'")
    if os.environ.get("OSMSG_DUCKDB_PRESERVE_ORDER", "").lower() in {"false", "0", "no"}:
        conn.execute("SET preserve_insertion_order=false")


def connect(db_path: str) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(db_path)
    _apply_runtime_pragmas(conn)
    return conn


def close(conn: duckdb.DuckDBPyConnection) -> None:
    conn.close()


def create_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    for stmt in DUCKDB_SCHEMA.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)


def upsert_state(conn: duckdb.DuckDBPyConnection, *, source_url: str, last_seq: int, last_ts, updated_at) -> None:
    conn.execute(
        """
        INSERT INTO state (source_url, last_seq, last_ts, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (source_url) DO UPDATE SET
            last_seq   = EXCLUDED.last_seq,
            last_ts    = EXCLUDED.last_ts,
            updated_at = EXCLUDED.updated_at
        """,
        [source_url, last_seq, last_ts, updated_at],
    )


def get_state(conn: duckdb.DuckDBPyConnection, source_url: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT last_seq, last_ts, updated_at FROM state WHERE source_url = ?",
        [source_url],
    ).fetchone()
    if row is None:
        return None
    return {"last_seq": row[0], "last_ts": row[1], "updated_at": row[2]}


__all__ = ["close", "connect", "create_tables", "get_state", "upsert_state"]
