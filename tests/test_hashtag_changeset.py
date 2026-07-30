"""The hashtag_changeset builder: per-changeset counts summed across seqs, tag_stats merged."""

from __future__ import annotations

import duckdb
import pytest

from osmsg.maintain.rollup import build_hashtag_changeset_table
from osmsg.stats import COUNT_COLS


@pytest.fixture
def store():
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial")
    cols = ", ".join(f"{c} BIGINT" for c in COUNT_COLS)
    con.execute(
        f"CREATE TABLE changeset_stats (changeset_id BIGINT, seq_id BIGINT, uid BIGINT, {cols}, "
        "tags STRUCT(k VARCHAR, v VARCHAR, c BIGINT, m BIGINT, l DOUBLE)[])"
    )
    b1 = "[{'k':'building','v':'yes','c':2,'m':1,'l':NULL}]"
    b2 = "[{'k':'building','v':'yes','c':3,'m':0,'l':NULL},{'k':'highway','v':'residential','c':1,'m':0,'l':NULL}]"
    # changeset 1: TWO seq rows (long-open changeset across diffs) -> must sum + merge tags.
    con.execute(
        f"""INSERT INTO changeset_stats VALUES
        (1, 0, 7, 10, 0,0, 3,0,0, 0,0,0, 0,0, {b1}),
        (1, 1, 7,  5, 0,0, 4,0,0, 0,0,0, 0,0, {b2}),
        (2, 0, 9,  8, 0,0, 1,0,0, 0,0,0, 0,0, NULL)
        """
    )
    con.execute(
        "CREATE TABLE changesets "
        "(changeset_id BIGINT, uid BIGINT, created_at TIMESTAMP, hashtags VARCHAR[], editor VARCHAR, geom GEOMETRY)"
    )
    con.execute(
        """INSERT INTO changesets VALUES
        (1, 7, '2024-01-01', ['#hotosm-project-1','#HOTOSM-fanclub'], 'iD', ST_Point(85.3, 27.7)),
        (2, 9, '2024-01-02', ['#missingmaps'], 'JOSM', NULL)
        """
    )
    return con


def test_build_hashtag_changeset(store):
    build_hashtag_changeset_table(store)

    # changeset 1 has two hashtags -> two rows, both lowercased.
    rows = store.execute(
        "SELECT hashtag, changeset_id, nodes_created, ways_created FROM hashtag_changeset ORDER BY hashtag"
    ).fetchall()
    assert rows == [
        ("#hotosm-fanclub", 1, 15, 7),  # summed across seq 0+1: nodes 10+5, ways 3+4
        ("#hotosm-project-1", 1, 15, 7),
        ("#missingmaps", 2, 8, 1),
    ]

    # tags merged across seqs for changeset 1: building/yes c=2+3=5, m=1; highway/residential c=1.
    tags = store.execute(
        """SELECT t.k, t.v, t.c, t.m FROM (
               SELECT UNNEST(tags) AS t
               FROM (SELECT DISTINCT ON (changeset_id) tags FROM hashtag_changeset WHERE changeset_id=1)
           ) ORDER BY t.k, t.v"""
    ).fetchall()
    assert tags == [("building", "yes", 5, 1), ("highway", "residential", 1, 0)]


def test_sorted_by_hashtag(store):
    build_hashtag_changeset_table(store)
    hashtags = [r[0] for r in store.execute("SELECT hashtag FROM hashtag_changeset").fetchall()]
    assert hashtags == sorted(hashtags)  # physically sorted so a prefix range prunes
