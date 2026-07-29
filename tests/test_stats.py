"""The shared stats vocabulary must compute the right numbers, proven on DuckDB."""

from __future__ import annotations

import duckdb
import pytest

from osmsg.stats import (
    COUNT_COLS,
    MAP_CHANGES_COLS,
    map_changes_expr,
    map_changes_sum,
    prefix_upper_bound,
    sum_cols,
)


def test_map_changes_excludes_poi():
    assert "poi_created" not in MAP_CHANGES_COLS
    assert "poi_modified" not in MAP_CHANGES_COLS
    assert set(MAP_CHANGES_COLS) == {c for c in COUNT_COLS if not c.startswith("poi")}
    assert COUNT_COLS[:9] == MAP_CHANGES_COLS


@pytest.fixture
def stats_conn():
    con = duckdb.connect()
    cols = ", ".join(f"{c} INTEGER" for c in COUNT_COLS)
    con.execute(f"CREATE TABLE cs (changeset_id BIGINT, uid BIGINT, {cols})")
    # uid 1: two changesets; uid 2: one. Distinct values per column so a wrong column shows up.
    con.execute(
        """
        INSERT INTO cs VALUES
        (1, 1, 10,1,0, 5,0,0, 0,0,0, 3,0),
        (2, 1, 20,2,1, 4,1,0, 1,0,0, 2,1),
        (3, 2,  7,0,0, 2,0,0, 0,0,0, 1,0)
        """
    )
    return con


def test_sum_cols_and_map_changes(stats_conn):
    sql = f"SELECT uid, {sum_cols('cs')}, {map_changes_sum('cs')} FROM cs GROUP BY uid ORDER BY uid"
    rows = {r[0]: r for r in stats_conn.execute(sql).fetchall()}
    cols = [d[0] for d in stats_conn.execute(sql).description]
    u1 = dict(zip(cols, rows[1], strict=True))
    # uid 1: nodes_created 10+20=30, ways_created 5+4=9, poi_created 3+2=5
    assert u1["nodes_created"] == 30
    assert u1["ways_created"] == 9
    assert u1["poi_created"] == 5
    # map_changes = all 9 non-poi cols summed, poi excluded: (10+1+0+5+0+0+0+0+0)+(20+2+1+4+1+0+1+0+0) = 16 + 29 = 45
    assert u1["map_changes"] == 45


def test_map_changes_expr_is_nine_terms():
    assert map_changes_expr("cs").count("+") == 8  # nine columns, eight plus signs
    assert "poi" not in map_changes_expr("cs")


def test_tag_breakdown_from_native_list(stats_conn):
    # tag_breakdown_from_list aggregates the native LIST<STRUCT(k,v,c,m,l)> tags column.
    from osmsg.stats import TAG_STRUCT_DDL, tag_breakdown_from_list

    stats_conn.execute(f"CREATE TABLE tl (changeset_id BIGINT, tags {TAG_STRUCT_DDL}[])")
    stats_conn.execute(
        """INSERT INTO tl VALUES
        (1, [{'k':'building','v':'yes','c':5,'m':2,'l':NULL}]),
        (2, [{'k':'building','v':'yes','c':3,'m':1,'l':NULL},
             {'k':'highway','v':'residential','c':1,'m':0,'l':NULL}])"""
    )
    rows = {(r[0], r[1]): (r[2], r[3]) for r in stats_conn.execute(tag_breakdown_from_list("tl")).fetchall()}
    assert rows[("building", "yes")] == (8, 3)
    assert rows[("highway", "residential")] == (1, 0)


def test_prefix_upper_bound():
    assert prefix_upper_bound("#hotosm") == "#hotosn"
    assert prefix_upper_bound("a") == "b"
    with pytest.raises(ValueError, match="non-empty"):
        prefix_upper_bound("")
