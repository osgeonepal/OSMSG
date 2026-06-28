"""Generate one finished month of the published datasets from the live day diffs, export the two
parquet partitions, and optionally upload them to HuggingFace."""

import calendar
import datetime as dt
import pathlib
import subprocess

import duckdb

from ..exceptions import OsmsgError
from .parquet import GEOM_COLS, MORTON_MACROS, write_partitions

UTC = dt.UTC
COMPLETENESS_TOLERANCE = dt.timedelta(hours=1)


def _month_bounds(year: int, month: int) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime(year, month, 1, tzinfo=UTC)
    end = dt.datetime(year + 1, 1, 1, tzinfo=UTC) if month == 12 else dt.datetime(year, month + 1, 1, tzinfo=UTC)
    return start, end


def ensure_complete_month(year: int, month: int) -> None:
    """Refuse a month that has not fully elapsed; only finished months are appended to the dataset."""
    last_day = calendar.monthrange(year, month)[1]
    if dt.datetime(year, month, last_day, tzinfo=UTC).date() >= dt.datetime.now(UTC).date():
        raise ValueError(f"{year:04d}-{month:02d} is not a complete month yet; only append finished months.")


def verify_month_complete(
    db: pathlib.Path, year: int, month: int, tolerance: dt.timedelta = COMPLETENESS_TOLERANCE
) -> None:
    """Raise if the generated month stops short of its boundary, so a partial month is never published
    as complete. Re-running once the day diffs cover the full month produces a complete partition."""
    _, month_end = _month_bounds(year, month)
    con = duckdb.connect(str(db), read_only=True)
    row = con.execute("SELECT max(created_at) FROM changesets").fetchone()
    con.close()
    latest = row[0] if row else None
    if latest is None:
        raise OsmsgError(f"{year:04d}-{month:02d}: no changesets generated.")
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    gap = month_end - latest
    if gap > tolerance:
        raise OsmsgError(
            f"{year:04d}-{month:02d} is incomplete: data ends {latest.astimezone(UTC).isoformat()}, "
            f"{gap} before the month boundary. The day diffs are likely lagging; retry once they cover "
            f"the full month, or pass --allow-incomplete to publish it anyway."
        )


def generate_month(year: int, month: int, work: pathlib.Path) -> pathlib.Path:
    """Run osmsg over the whole month from day replication and return the produced .duckdb path."""
    from ..pipeline import RunConfig, run

    start, end = _month_bounds(year, month)
    name = f"upd{year:04d}{month:02d}"
    work.mkdir(parents=True, exist_ok=True)
    run(
        RunConfig(
            name=name,
            start_date=start,
            end_date=end,
            urls=["day"],
            url_explicit=True,
            tag_mode="all",
            history_mode="off",
            formats=["parquet"],
            output_dir=work,
        )
    )
    return work / f"{name}.duckdb"


def export_month(db: pathlib.Path, year: int, month: int, out: pathlib.Path) -> tuple[int, int]:
    """Export the month's changefiles/changesets partitions, Morton-sorted, and return their row counts."""
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; INSTALL json; LOAD json;")
    con.execute(MORTON_MACROS)
    con.execute(f"ATTACH '{db}' AS m (READ_ONLY)")
    where = f"year(c.created_at)={year} AND month(c.created_at)={month}"
    con.execute(
        f"""CREATE TABLE changefiles_all AS
            SELECT s.* EXCLUDE (seq_id), c.created_at, {GEOM_COLS},
                   year(c.created_at) y, month(c.created_at) m
            FROM m.changeset_stats s JOIN m.changesets c USING (changeset_id) WHERE {where}"""
    )
    con.execute(
        f"""CREATE TABLE changesets_all AS
            SELECT c.changeset_id, c.uid, u.username, c.created_at, c.editor, c.hashtags, {GEOM_COLS},
                   year(c.created_at) y, month(c.created_at) m
            FROM m.changesets c LEFT JOIN m.users u USING (uid) WHERE {where}"""
    )
    write_partitions(con, "changefiles_all", out / "changefiles")
    write_partitions(con, "changesets_all", out / "changesets")
    cf = _count(con, "changefiles_all")
    cs = _count(con, "changesets_all")
    con.close()
    return cf, cs


def _count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    row = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    return row[0] if row else 0


def upload(repo: str, out: pathlib.Path, year: int, month: int) -> None:
    """Upload both month partitions to the HuggingFace dataset repo via the hf CLI."""
    for dataset in ("changefiles", "changesets"):
        local = out / dataset / f"year={year}" / f"month={month}"
        remote = f"{dataset}/year={year}/month={month}"
        subprocess.run(
            ["uvx", "--from", "huggingface_hub", "hf", "upload", repo, str(local), remote, "--repo-type", "dataset"],
            check=True,
        )
