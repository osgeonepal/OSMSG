"""DuckDB persistence: schema, ingest, queries.

The schema is portable: identical column shape works in DuckDB, Parquet, and
PostgreSQL, exporters re-issue the CREATE TABLE there.

Public surface:

    >>> from osmsg.db import connect, create_tables, user_stats
    >>> conn = connect("stats.duckdb")
    >>> create_tables(conn)
    >>> rows = user_stats(conn, top_n=10)
"""

from .ingest import flush_rows_to_parquet, merge_parquet_files
from .queries import (
    attach_metadata,
    attach_tag_stats,
    daily_summary,
    list_changesets,
    user_stats,
)
from .schema import (
    close,
    connect,
    create_tables,
    get_state,
    upsert_state,
)

__all__ = [
    "attach_metadata",
    "attach_tag_stats",
    "close",
    "connect",
    "create_tables",
    "daily_summary",
    "flush_rows_to_parquet",
    "get_state",
    "list_changesets",
    "merge_parquet_files",
    "upsert_state",
    "user_stats",
]
