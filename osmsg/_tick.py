"""Worker tick: bootstrap on first run, --update thereafter."""

import fcntl
import os
import shlex
import subprocess
import sys
from pathlib import Path

import duckdb

from .db import connect, create_tables, get_state, upsert_state
from .db.pg import attach_postgres
from .geofabrik import country_update_url
from .replication import resolve_url


def _has_state(db_path: Path, source_url: str) -> bool:
    if not db_path.exists():
        return False
    conn = connect(str(db_path))
    create_tables(conn)
    result = get_state(conn, source_url) is not None
    conn.close()
    return result


def _has_any_state(db_path: Path) -> bool:
    """True if the store has a resume position for ANY source, so `--update` continues (and auto-refines)
    it instead of re-bootstrapping."""
    if not db_path.exists():
        return False
    conn = connect(str(db_path))
    create_tables(conn)
    row = conn.execute("SELECT count(*) FROM state").fetchone()
    conn.close()
    return bool(row and row[0])


def _parse_arg(args: list[str], flag: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def _reset_store_buffer(db_path: Path) -> None:
    """Drop the store's data tables and recreate them empty, keeping `state`, so the store holds only
    the next tick's delta rather than every row pushed since the last reset."""
    if not db_path.exists():
        return
    conn = connect(str(db_path))
    for table in ("changeset_stats", "changesets", "users"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    create_tables(conn)
    conn.close()


def _read_pg_state(dsn: str) -> list[tuple]:
    """Read the resume state rows from the Postgres permanent copy (read-only), used to re-seed a rebuilt
    store. The DSN is interpolated into ATTACH, so it must be trusted (same contract as export.psql)."""
    conn = duckdb.connect()
    try:
        attach_postgres(conn, dsn, read_only=True)
        rows = conn.execute("SELECT source_url, last_seq, last_ts, updated_at FROM pg.state").fetchall()
        conn.execute("DETACH pg")
    finally:
        conn.close()
    return rows


def _store_is_dirty(db_path: Path) -> bool:
    """A psql tick must start from an empty delta buffer (the last successful push reset it). Leftover data
    means the previous push was interrupted, and the abrupt stop can also leave the store's index corrupt.
    An unreadable store is treated as dirty so it gets rebuilt rather than crashing the run."""
    if not db_path.exists():
        return False
    try:
        conn = connect(str(db_path))
    except duckdb.Error:
        return True
    try:
        create_tables(conn)
        row = conn.execute("SELECT count(*) FROM changeset_stats").fetchone()
        return bool(row) and row[0] > 0
    except duckdb.Error:
        return True
    finally:
        conn.close()


def _rebuild_store_from_pg(db_path: Path, dsn: str) -> None:
    """Discard a dirty or corrupt delta buffer and rebuild it fresh, re-seeding the resume state from the
    Postgres permanent copy so `--update` continues from the last durably pushed position: no gap, no
    double-count (the push is ON CONFLICT DO NOTHING), and a clean index. This is the automatic recovery
    that replaces manual store surgery after an interrupted push."""
    pg_state = _read_pg_state(dsn)
    for path in (db_path, db_path.with_name(db_path.name + ".wal")):
        if path.exists():
            path.unlink()
    conn = connect(str(db_path))
    try:
        create_tables(conn)
        for source_url, last_seq, last_ts, updated_at in pg_state:
            upsert_state(conn, source_url=source_url, last_seq=last_seq, last_ts=last_ts, updated_at=updated_at)
    finally:
        conn.close()


# `or` not a default arg: compose passes the var as an empty string when unset, which get(default) keeps.
_TICK_TIMEOUT_SECONDS = int(os.environ.get("OSMSG_TICK_TIMEOUT_SECONDS") or "1200")


def main() -> int:
    extra_args = shlex.split(os.environ.get("OSMSG_EXTRA_ARGS", ""))
    bootstrap_days = os.environ.get("OSMSG_BOOTSTRAP_DAYS", "1")
    name = _parse_arg(extra_args, "--name") or "stats"
    out = Path(_parse_arg(extra_args, "--output-dir") or "/var/lib/osmsg")
    country = _parse_arg(extra_args, "--country")
    explicit_url = _parse_arg(extra_args, "--url")
    url = explicit_url or "day"

    out.mkdir(parents=True, exist_ok=True)

    lock_path = out / f"{name}.lock"
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        print("[osmsg-tick] previous tick still running, skipping", flush=True)
        return 0

    try:
        # Mirror pipeline._normalize_urls: explicit --url wins over --country's geofabrik default,
        # otherwise --update can't find the state row and the DuckDB gets wiped every tick.
        source_url = country_update_url(country) if country and explicit_url is None else resolve_url(url)
        db_path = out / f"{name}.duckdb"
        psql_dsn = _parse_arg(extra_args, "--psql-dsn")

        if psql_dsn and _store_is_dirty(db_path):
            print("[osmsg-tick] store dirty from an interrupted push; rebuilding from Postgres state", flush=True)
            _rebuild_store_from_pg(db_path, psql_dsn)

        extra_set = set(extra_args)
        cmd = ["osmsg"] + extra_args
        if not (extra_set & {"--all", "--keys"}):
            cmd.append("--all")

        # Country tracks one geofabrik source; the planet run continues whatever was seeded, so accept any.
        has_state = _has_state(db_path, source_url) if country else _has_any_state(db_path)
        if has_state:
            cmd.append("--update")
        else:
            # Cold start at day granularity; --update then refines to hour/minute as the backlog shrinks.
            if explicit_url is None and not country:
                cmd.extend(["--url", "day"])
            cmd.extend(["--days", bootstrap_days])

        print(f"[osmsg-tick] {' '.join(cmd)}", flush=True)
        try:
            rc = subprocess.call(cmd, timeout=_TICK_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            print(f"[osmsg-tick] killed: exceeded {_TICK_TIMEOUT_SECONDS}s", flush=True)
            rc = 1
        # With a psql push, Postgres is the permanent copy and the store is a per-tick delta buffer: clear
        # its data (keeping resume `state`) after a successful push so the next push stays small and fast.
        if rc == 0 and psql_dsn:
            _reset_store_buffer(db_path)
        return rc
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
