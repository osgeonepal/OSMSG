"""The catalog combines the published rollup (history, before the frontier) with the recent tail
derived on the fly from the base tables (from the frontier on). The split at the frontier must be
exact: no changeset counted twice, none dropped."""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest

from osmsg import catalog
from osmsg.catalog import hashtag_scope
from osmsg.stats import COUNT_COLS, map_changes_sum, prefix_upper_bound

_FRONTIER = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)


def _make(con):
    """A published rollup `hc` and the matching base tables (`cs_stats`, `csets`). The same four
    changesets appear in both so a whole-vs-split comparison is meaningful: 1 is history (< frontier),
    2 and 3 are recent #hotosm (>= frontier), 4 is a recent non-match."""
    cols = ", ".join(f"{c} BIGINT" for c in COUNT_COLS)
    zeros = ", ".join(["0"] * len(COUNT_COLS))
    con.execute(
        f"CREATE TABLE hc (hashtag VARCHAR, changeset_id BIGINT, uid BIGINT, editor VARCHAR, "
        f"created_at TIMESTAMP, {cols}, tags STRUCT(k VARCHAR, v VARCHAR, c BIGINT, m BIGINT, len_m DOUBLE)[])"
    )
    con.execute(
        f"""INSERT INTO hc VALUES
        ('#hotosm-project-1', 1, 7, 'iD', '2026-06-15', {zeros.replace("0", "5", 1)}, []),
        ('#hotosm-project-2', 2, 8, 'iD', '2026-07-05', {zeros.replace("0", "9", 1)}, []),
        ('#hotosm-fanclub',   3, 9, 'JOSM', '2026-07-20', {zeros.replace("0", "3", 1)}, []),
        ('#missingmaps',      4, 9, 'iD', '2026-07-06', {zeros}, [])
        """
    )
    con.execute(
        f"CREATE TABLE cs_stats (changeset_id BIGINT, seq_id BIGINT, uid BIGINT, {cols}, "
        "tags STRUCT(k VARCHAR, v VARCHAR, c BIGINT, m BIGINT, len_m DOUBLE)[])"
    )
    con.execute(
        f"""INSERT INTO cs_stats VALUES
        (1, 0, 7, {zeros.replace("0", "5", 1)}, NULL),
        (2, 0, 8, {zeros.replace("0", "9", 1)}, NULL),
        (3, 0, 9, {zeros.replace("0", "3", 1)}, NULL),
        (4, 0, 9, {zeros}, NULL)
        """
    )
    con.execute(
        "CREATE TABLE csets (changeset_id BIGINT, uid BIGINT, editor VARCHAR, created_at TIMESTAMP, hashtags VARCHAR[])"
    )
    con.execute(
        """INSERT INTO csets VALUES
        (1, 7, 'iD', '2026-06-15', ['#hotosm-project-1']),
        (2, 8, 'iD', '2026-07-05', ['#hotosm-project-2']),
        (3, 9, 'JOSM', '2026-07-20', ['#hotosm-fanclub']),
        (4, 9, 'iD', '2026-07-06', ['#missingmaps'])
        """
    )


def _summary(con, sql, params):
    r = con.execute(f"WITH m AS ({sql}) SELECT count(DISTINCT uid) u, count(*) n, {map_changes_sum()} FROM m", params)
    return r.fetchone()


def test_combine_split_equals_whole():
    con = duckdb.connect()
    _make(con)
    lo, hi = "#hotosm", prefix_upper_bound("#hotosm")

    whole = con.execute(
        f"SELECT count(DISTINCT uid), count(*), {map_changes_sum()} "
        "FROM (SELECT DISTINCT ON (changeset_id) * FROM hc WHERE hashtag >= ? AND hashtag < ?)",
        [lo, hi],
    ).fetchone()
    # History from the rollup (< frontier), recent from the base tables (>= frontier): reproduces the whole.
    sql, params = hashtag_scope("hc", "cs_stats", "csets", prefixes=[(lo, hi)], frontier=_FRONTIER)
    combined = _summary(con, sql, params)
    assert combined == whole
    assert whole[1] == 3  # three #hotosm changesets, #missingmaps excluded


def test_recent_side_only_counts_after_frontier():
    con = duckdb.connect()
    _make(con)
    lo, hi = "#hotosm", prefix_upper_bound("#hotosm")
    # Empty history -> only the two #hotosm changesets on/after the frontier (2 and 3) remain.
    con.execute("CREATE TABLE empty_hist AS SELECT * FROM hc LIMIT 0")
    sql, params = hashtag_scope("empty_hist", "cs_stats", "csets", prefixes=[(lo, hi)], frontier=_FRONTIER)
    combined = _summary(con, sql, params)
    assert combined[1] == 2  # changesets 2 and 3 (2026-07-05, 2026-07-20)


def test_window_bounds_both_sides_of_frontier():
    con = duckdb.connect()
    _make(con)
    lo, hi = "#hotosm", prefix_upper_bound("#hotosm")

    def window(start, end):
        sql, params = hashtag_scope(
            "hc", "cs_stats", "csets", prefixes=[(lo, hi)], frontier=_FRONTIER, start=start, end=end
        )
        return _summary(con, sql, params)[1]  # changeset count

    # recent-only window: just cs2 (2026-07-05); cs1 before start, cs3 after end.
    assert window(dt.datetime(2026, 7, 1, tzinfo=dt.UTC), dt.datetime(2026, 7, 10, tzinfo=dt.UTC)) == 1
    # window straddling the frontier: cs1 (history) + cs2 (recent); cs3 excluded by end.
    assert window(dt.datetime(2026, 6, 1, tzinfo=dt.UTC), dt.datetime(2026, 7, 10, tzinfo=dt.UTC)) == 2
    # history-only window: cs1 only; end precedes the frontier so the base side is empty.
    assert window(dt.datetime(2026, 6, 1, tzinfo=dt.UTC), dt.datetime(2026, 6, 20, tzinfo=dt.UTC)) == 1
    # open upper bound: cs1 + cs2 + cs3 (all #hotosm from start on).
    assert window(dt.datetime(2026, 6, 1, tzinfo=dt.UTC), None) == 3


def test_multi_prefix_union_and_dedup():
    con = duckdb.connect()
    _make(con)
    h = prefix_upper_bound
    # union of two disjoint prefixes: all four changesets (1,2,3 #hotosm + 4 #missingmaps).
    disjoint = [("#hotosm", h("#hotosm")), ("#missingmaps", h("#missingmaps"))]
    sql, params = hashtag_scope("hc", "cs_stats", "csets", prefixes=disjoint, frontier=_FRONTIER)
    assert _summary(con, sql, params)[1] == 4
    # overlapping prefixes must not double-count: #hotosm already covers #hotosm-project.
    overlap = [("#hotosm", h("#hotosm")), ("#hotosm-project", h("#hotosm-project"))]
    sql, params = hashtag_scope("hc", "cs_stats", "csets", prefixes=overlap, frontier=_FRONTIER)
    assert _summary(con, sql, params)[1] == 3  # the three #hotosm changesets, none doubled


def test_map_scope_centroids_across_frontier():
    from osmsg.catalog import map_scope

    con = duckdb.connect()
    # history rollup carries lon/lat directly; recent changesets carry a bbox (centroid = midpoint).
    con.execute(
        "CREATE TABLE hc_geo (hashtag VARCHAR, changeset_id BIGINT, uid BIGINT, created_at TIMESTAMP, "
        "lon DOUBLE, lat DOUBLE)"
    )
    con.execute(
        """INSERT INTO hc_geo VALUES
        ('#hotosm-project-1', 1, 7, '2026-06-15', 85.3, 27.7),
        ('#hotosm-fanclub',   1, 7, '2026-06-15', 85.3, 27.7),
        ('#hotosm-project-2', 2, 8, '2026-06-20', 10.0, 20.0)"""
    )
    con.execute(
        "CREATE TABLE cs_geo (changeset_id BIGINT, uid BIGINT, created_at TIMESTAMP, hashtags VARCHAR[], "
        "min_lon DOUBLE, min_lat DOUBLE, max_lon DOUBLE, max_lat DOUBLE)"
    )
    con.execute(
        """INSERT INTO cs_geo VALUES
        (3, 9, '2026-07-20', ['#hotosm-fanclub'], 0.0, 0.0, 4.0, 8.0),
        (4, 9, '2026-07-06', ['#missingmaps'], 1.0, 1.0, 1.0, 1.0)"""
    )
    lo, hi = "#hotosm", prefix_upper_bound("#hotosm")
    sql, params = map_scope("hc_geo", "cs_geo", prefixes=[(lo, hi)], frontier=_FRONTIER)
    rows = {r[0]: (r[2], r[3]) for r in con.execute(sql, params).fetchall()}
    # cs1 history (deduped despite two hashtag rows), cs2 history, cs3 recent centroid=(2,4); cs4 excluded.
    assert set(rows) == {1, 2, 3}
    assert rows[1] == (85.3, 27.7)
    assert rows[3] == (2.0, 4.0)  # bbox midpoint of (0,0)-(4,8)


def test_history_side_ignores_recent_rollup_rows():
    con = duckdb.connect()
    _make(con)
    lo, hi = "#hotosm", prefix_upper_bound("#hotosm")
    # The rollup also holds changesets 2 and 3 (>= frontier); the history side must exclude them so the
    # recent base side owns them (no double-count). Only changeset 1 comes from history here.
    sql, params = hashtag_scope("hc", "cs_stats", "csets", prefixes=[(lo, hi)], frontier=_FRONTIER)
    ids = con.execute(f"WITH m AS ({sql}) SELECT changeset_id FROM m ORDER BY changeset_id", params).fetchall()
    assert [r[0] for r in ids] == [1, 2, 3]


def test_recent_pg_aggregates_shape_and_bounds():
    # Each recent aggregate is a Postgres passthrough over the changeset_hashtag prefix index, with the
    # frontier + window inlined and the endpoint's grain in the SELECT.
    lo, hi = "#hotosm", prefix_upper_bound("#hotosm")
    start = dt.datetime(2026, 7, 10, tzinfo=dt.UTC)
    end = dt.datetime(2026, 7, 20, tzinfo=dt.UTC)
    kw = dict(prefixes=[(lo, hi)], frontier=_FRONTIER, start=start, end=end)
    user = catalog.recent_user_agg("pg", **kw)
    for probe in ("postgres_query('pg'", "changeset_hashtag", 'COLLATE "C"', lo, hi, "2026-07-10", "2026-07-20"):
        assert probe in user
    assert "2026-07-01" in user  # frontier lower bound
    assert "GROUP BY uid" in user
    assert "creates" in catalog.recent_tag_agg("pg", **kw)
    assert "editors" in catalog.recent_leaderboard_agg("pg", **kw)
    assert "editor" in catalog.recent_editor_agg("pg", **kw)
    assert "bucket" in catalog.recent_bucket_agg("pg", "day", **kw)
    assert "lon" in catalog.recent_map_agg("pg", **kw)
    assert "hashtag" in catalog.recent_hashtag_agg("pg", **kw)


def test_recent_pg_aggregate_escapes_quotes():
    # A hashtag carrying a quote must not break out of the inlined literal (PG + DuckDB double-escaping).
    sql = catalog.recent_user_agg("pg", prefixes=[("#a'b", "#a'c")], frontier=_FRONTIER)
    assert "a''''b" in sql and "a''''c" in sql
    assert "#a'b'" not in sql  # no lone unescaped quote that would terminate the literal early


def test_recent_pg_aggregate_requires_prefixes():
    with pytest.raises(ValueError):
        catalog.recent_user_agg("pg", prefixes=[], frontier=_FRONTIER)
