"""Per-user rollups so the API can serve any window, hashtag, or tag query from small precomputed
aggregates instead of scanning the full history. Built for each finished month by `maintain month`.
See docs/rollups.md."""

import pathlib

import duckdb

from ..db.schema import _apply_runtime_pragmas
from ..exceptions import OsmsgError
from ..stats import COUNT_COLS as _COUNT_COLS
from .parquet import ROW_GROUP_SIZE


def hashtag_changeset_select(stats_rel: str = "changeset_stats", changesets_rel: str = "changesets") -> str:
    """SELECT expanding the store into one row per (hashtag, changeset): counts summed across a changeset's
    seq rows and tags merged, correct for both per-diff (live) and seq_id=0 (history) rows. `lon`/`lat` are
    the bbox centroid; sorted by lowercased hashtag so a prefix range prunes row groups."""
    sums = ", ".join(f"SUM(s.{c}) AS {c}" for c in _COUNT_COLS)
    payload = ", ".join(f"p.{c}" for c in _COUNT_COLS)
    return f"""
        WITH per_cs AS (
            SELECT changeset_id, any_value(uid) AS uid, {sums}
            FROM {stats_rel} s GROUP BY changeset_id
        ),
        tag_rows AS (
            SELECT changeset_id, t.k AS k, t.v AS v,
                   SUM(t.c) AS c, SUM(t.m) AS m, SUM(t.l) AS l
            FROM (SELECT changeset_id, UNNEST(tags) AS t FROM {stats_rel} WHERE tags IS NOT NULL AND len(tags) > 0)
            GROUP BY changeset_id, t.k, t.v
        ),
        merged_tags AS (
            SELECT changeset_id, list(struct_pack(k := k, v := v, c := c, m := m, l := l)) AS tags
            FROM tag_rows GROUP BY changeset_id
        )
        SELECT lower(h) AS hashtag, p.changeset_id, p.uid, c.editor, c.created_at, {payload},
               COALESCE(t.tags, []) AS tags,
               ST_X(ST_Centroid(c.geom)) AS lon, ST_Y(ST_Centroid(c.geom)) AS lat
        FROM per_cs p
        JOIN {changesets_rel} c USING (changeset_id)
        LEFT JOIN merged_tags t USING (changeset_id)
        CROSS JOIN UNNEST(c.hashtags) AS u(h)
        WHERE c.hashtags IS NOT NULL
    """


def build_hashtag_changeset_table(con: duckdb.DuckDBPyConnection) -> None:
    """Materialize the hashtag-sorted rollup as a table in the store for fast hashtag queries."""
    con.execute(f"CREATE OR REPLACE TABLE hashtag_changeset AS {hashtag_changeset_select()} ORDER BY hashtag")


def write_hashtag_changeset_parquet(con: duckdb.DuckDBPyConnection, out: pathlib.Path) -> None:
    """Write the hashtag rollup to a single hashtag-sorted parquet (for publishing)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY ({hashtag_changeset_select()} ORDER BY hashtag) "
        f"TO '{out}' (FORMAT parquet, ROW_GROUP_SIZE {ROW_GROUP_SIZE})"
    )


def _copy(con: duckdb.DuckDBPyConnection, path: pathlib.Path, select: str, order_by: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({select} ORDER BY {order_by}) TO '{path}' (FORMAT parquet, ROW_GROUP_SIZE {ROW_GROUP_SIZE})")


def build_month_rollups(year: int, month: int, out: pathlib.Path) -> None:
    """Build the hashtag_changeset and users rollups for one month from its raw partitions. Idempotent:
    a re-run replaces the month's rows."""
    changefiles = out / "changefiles" / f"year={year}" / f"month={month}" / "data.parquet"
    changesets = out / "changesets" / f"year={year}" / f"month={month}" / "data.parquet"
    if not changefiles.exists() or not changesets.exists():
        raise OsmsgError(f"missing raw partition for {year:04d}-{month:02d}; export the month first")

    con = duckdb.connect()
    _apply_runtime_pragmas(con)
    con.execute(f"CREATE VIEW cf AS SELECT * FROM read_parquet('{changefiles}')")
    con.execute(f"CREATE VIEW cs AS SELECT * FROM read_parquet('{changesets}')")

    _refresh_hashtag_changeset(con, out)
    _refresh_users(con, out)
    con.close()


def _refresh_hashtag_changeset(con: duckdb.DuckDBPyConnection, out: pathlib.Path) -> None:
    """Merge this month's (hashtag, changeset) rows into the single hashtag-sorted file, idempotent (a
    re-run replaces the month). changefiles is already per-changeset, so no seq aggregation; sorted by
    hashtag and kept as one file so a prefix range prunes and a remote query opens one footer."""
    hc = out / "rollup" / "hashtag_changeset" / "data.parquet"
    cf_counts = ", ".join(f"cf.{c}" for c in _COUNT_COLS)
    con.execute(
        f"""CREATE TABLE month_hc AS
            SELECT lower(h) AS hashtag, cf.changeset_id, cf.uid, cs.editor, cf.created_at,
                   {cf_counts}, cf.tags, cs.lon, cs.lat
            FROM cf JOIN cs USING (changeset_id) CROSS JOIN UNNEST(cs.hashtags) AS t(h)
            WHERE cs.hashtags IS NOT NULL"""
    )
    if hc.exists():
        con.execute(f"CREATE TABLE prev_hc AS SELECT * FROM read_parquet('{hc}')")
        select = (
            "SELECT * FROM prev_hc WHERE changeset_id NOT IN (SELECT changeset_id FROM month_hc) "
            "UNION ALL SELECT * FROM month_hc"
        )
    else:
        select = "SELECT * FROM month_hc"
    _copy(con, hc, select, "hashtag")


def _refresh_users(con: duckdb.DuckDBPyConnection, out: pathlib.Path) -> None:
    """uid -> username. The month just built wins for its users; others keep their prior name."""
    users = out / "rollup" / "users" / "data.parquet"
    con.execute(
        "CREATE TABLE month_users AS "
        "SELECT uid, any_value(username) AS username FROM cs WHERE username IS NOT NULL GROUP BY uid"
    )
    if users.exists():
        con.execute(f"CREATE TABLE prev_users AS SELECT * FROM read_parquet('{users}')")
        select = (
            "SELECT uid, username FROM prev_users WHERE uid NOT IN (SELECT uid FROM month_users) "
            "UNION ALL SELECT uid, username FROM month_users"
        )
    else:
        select = "SELECT uid, username FROM month_users"
    _copy(con, users, select, "uid")
