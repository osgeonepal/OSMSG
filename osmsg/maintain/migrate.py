"""One-time migration: rewrite the published `changefiles` partitions from a JSON `tag_stats` column to
the native LIST<STRUCT> `tags` column, so every dataset uses the one native tag representation. Each
partition is read from HuggingFace, converted, and re-uploaded. Idempotent: partitions already native
are skipped, so an interrupted run resumes."""

import datetime as dt
import pathlib

import duckdb

from ..exceptions import OsmsgError
from ..history import fetch_manifest
from ..stats import TAG_STRUCT_DDL
from ..ui import info
from .month import _hf_upload
from .parquet import MORTON_MACROS, ROW_GROUP_SIZE


def _iter_months(start: dt.datetime, frontier: dt.datetime):
    year, month = start.year, start.month
    while dt.datetime(year, month, 1, tzinfo=dt.UTC) < frontier:
        yield year, month
        month += 1
        if month > 12:
            month, year = 1, year + 1


def _count(con: duckdb.DuckDBPyConnection, url: str) -> int:
    row = con.execute(f"SELECT count(*) FROM read_parquet('{url}')").fetchone()
    return row[0] if row else 0


def needs_migration(con: duckdb.DuckDBPyConnection, url: str) -> bool:
    """True when the partition still carries JSON `tag_stats` and not the native `tags` column."""
    names = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{url}')").fetchall()}
    return "tag_stats" in names and "tags" not in names


def convert_partition(con: duckdb.DuckDBPyConnection, url: str, dest: pathlib.Path, batches: int = 32) -> None:
    """Read a JSON-tag `changefiles` partition and write it Morton-sorted with the native `tags` column.
    Requires MORTON_MACROS loaded and the json extension. The tag explosion is done in changeset_id
    batches (a dense month explodes JSON into a multi-GB intermediate; batching keeps each pass small so
    a low memory_limit holds and the host is never at risk)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"CREATE OR REPLACE TEMP TABLE _mig_src AS SELECT * FROM read_parquet('{url}')")
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE _mig_out AS "
        f"SELECT * EXCLUDE (tag_stats), []::{TAG_STRUCT_DDL}[] AS tags FROM _mig_src LIMIT 0"
    )
    for b in range(batches):
        con.execute(
            f"""INSERT INTO _mig_out
                WITH tg AS (
                    SELECT changeset_id,
                           list(struct_pack(
                               k := tk.key, v := tv.key,
                               c := COALESCE((tv.value ->> 'c')::bigint, 0),
                               m := COALESCE((tv.value ->> 'm')::bigint, 0),
                               len_m := (tv.value ->> 'len')::double
                           )) AS tags
                    FROM _mig_src, json_each(tag_stats) AS tk, json_each(tk.value) AS tv
                    WHERE tag_stats IS NOT NULL AND changeset_id % {batches} = {b}
                    GROUP BY changeset_id
                )
                SELECT s.* EXCLUDE (tag_stats), COALESCE(tg.tags, []::{TAG_STRUCT_DDL}[]) AS tags
                FROM _mig_src s LEFT JOIN tg USING (changeset_id)
                WHERE s.changeset_id % {batches} = {b}"""
        )
    con.execute(
        f"""COPY (SELECT * FROM _mig_out ORDER BY morton2(lon, lat))
            TO '{dest.as_posix()}' (FORMAT parquet, ROW_GROUP_SIZE {ROW_GROUP_SIZE})"""
    )
    for tmp in ("_mig_src", "_mig_out"):
        con.execute(f"DROP TABLE IF EXISTS {tmp}")


def _connect(tmp_dir: str, memory_limit: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL json; LOAD json;")
    con.execute("SET http_keep_alive=true; SET http_timeout=120000; SET http_retries=5;")
    con.execute(f"SET memory_limit='{memory_limit}'")  # cap so a big month errors, never OOMs the host
    con.execute(f"SET temp_directory='{tmp_dir}'")  # spill the tag explosion to disk
    con.execute(MORTON_MACROS)
    return con


def migrate_changefiles_tags(repo: str, work_dir: pathlib.Path, memory_limit: str = "12GB") -> tuple[int, int]:
    """Convert every JSON-tag `changefiles` partition of `repo` to native tags in place. A fresh DuckDB
    connection per partition keeps memory from building up across the run. Returns (migrated, skipped)."""
    work_dir = pathlib.Path(work_dir)
    manifest = fetch_manifest(f"hf://datasets/{repo}")
    if manifest is None:
        raise OsmsgError(f"could not read the manifest for {repo}")
    tmp = work_dir / "duck_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    migrated = skipped = 0
    for year, month in _iter_months(manifest.min_month, manifest.frontier):
        url = f"hf://datasets/{repo}/changefiles/year={year}/month={month}/data.parquet"
        con = _connect(tmp.as_posix(), memory_limit)
        try:
            try:
                needs = needs_migration(con, url)
            except duckdb.Error:
                info(f"{year}-{month:02d}: no changefiles partition, skipping")
                continue
            if not needs:
                skipped += 1
                info(f"{year}-{month:02d}: already native, skipping")
                continue
            src_rows = _count(con, url)
            local = work_dir / f"year={year}" / f"month={month}" / "data.parquet"
            convert_partition(con, url, local)
            dest_rows = _count(con, local.as_posix())
            if dest_rows != src_rows:
                raise OsmsgError(f"{year}-{month:02d}: row count changed {src_rows} -> {dest_rows}; not uploading")
            _hf_upload(repo, local, f"changefiles/year={year}/month={month}/data.parquet")
            migrated += 1
            info(f"{year}-{month:02d}: migrated {dest_rows:,} rows to native and uploaded")
        finally:
            con.close()
    return migrated, skipped
