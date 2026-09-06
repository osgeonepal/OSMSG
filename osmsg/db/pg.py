"""Shared Postgres helpers for attaching a database to DuckDB and running native statements on it."""

import duckdb


def sql_literal(value: object) -> str:
    """Quote a value as a SQL string literal (NULL for None), doubling embedded single quotes."""
    return "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"


def attach_postgres(conn: duckdb.DuckDBPyConnection, dsn: str, *, alias: str = "pg", read_only: bool = False) -> None:
    """Attach a Postgres database to `conn` via the postgres extension. The DSN is interpolated, so it
    must be trusted."""
    conn.execute("INSTALL postgres")
    conn.execute("LOAD postgres")
    flags = "TYPE postgres, READ_ONLY" if read_only else "TYPE postgres"
    conn.execute(f"ATTACH {sql_literal(dsn)} AS {alias} ({flags})")


def connect_postgres(dsn: str, *, alias: str = "pg", read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """A fresh DuckDB connection with a Postgres database attached."""
    conn = duckdb.connect()
    attach_postgres(conn, dsn, alias=alias, read_only=read_only)
    return conn


def pg_execute(conn: duckdb.DuckDBPyConnection, sql: str, *, alias: str = "pg") -> None:
    """Run one statement natively on the attached Postgres so a bulk operation is a single indexed
    server-side statement. Dollar-quoted so the body's own quotes cannot terminate it."""
    conn.execute(f"CALL postgres_execute('{alias}', $osmsg_stmt${sql}$osmsg_stmt$)")
