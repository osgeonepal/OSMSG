"""The hashtag query surface combines history + recent and computes correct summary/leaderboard/tags."""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest

from osmsg import query
from osmsg.query import Sources
from osmsg.stats import COUNT_COLS


@pytest.fixture
def con():
    c = duckdb.connect()
    cols = ", ".join(f"{col} BIGINT" for col in COUNT_COLS)
    hist_ddl = (
        f"hashtag VARCHAR, changeset_id BIGINT, uid BIGINT, editor VARCHAR, created_at TIMESTAMP, "
        f"{cols}, tags STRUCT(k VARCHAR, v VARCHAR, c BIGINT, m BIGINT, len_m DOUBLE)[]"
    )
    zeros = ", ".join(["0"] * len(COUNT_COLS))
    b = "[{'k':'building','v':'yes','c':%d,'m':%d,'len_m':NULL}]"
    # history is the published rollup (native tags list); recent is the live base tables.
    c.execute(f"CREATE TABLE history ({hist_ddl})")
    c.execute(
        f"""INSERT INTO history VALUES
        ('#hotosm-project-1', 1, 1, 'iD', '2026-05-01', {zeros.replace("0", "10", 1)}, {b % (4, 0)}),
        ('#hotosm-project-1', 2, 2, 'JOSM', '2026-05-02', {zeros.replace("0", "6", 1)}, [])"""
    )
    c.execute(
        f"CREATE TABLE cs_stats (changeset_id BIGINT, seq_id BIGINT, uid BIGINT, {cols}, "
        "tags STRUCT(k VARCHAR, v VARCHAR, c BIGINT, m BIGINT, len_m DOUBLE)[])"
    )
    c.execute(
        f"""INSERT INTO cs_stats VALUES
        (3, 0, 1, {zeros.replace("0", "5", 1)}, [{{'k':'building','v':'yes','c':1,'m':2,'len_m':NULL}}])"""
    )
    c.execute(
        "CREATE TABLE csets (changeset_id BIGINT, uid BIGINT, editor VARCHAR, created_at TIMESTAMP, hashtags VARCHAR[])"
    )
    c.execute("INSERT INTO csets VALUES (3, 1, 'iD', '2026-07-05', ['#hotosm-project-2'])")
    c.execute("CREATE TABLE users AS SELECT * FROM (VALUES (1, 'alice'), (2, 'bob')) t(uid, username)")
    return c


@pytest.fixture
def sources():
    return Sources(
        history_rel="history",
        recent_stats_rel="cs_stats",
        recent_changesets_rel="csets",
        frontier=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
        users_rel="users",
    )


def test_summary_combines_history_and_recent(con, sources):
    s = query.summary(con, "hotosm", sources)
    assert s["users"] == 2  # user 1 (2 changesets) + user 2
    assert s["changesets"] == 3  # 2 history + 1 recent
    assert s["nodes_created"] == 21  # 10 + 6 + 5


def test_leaderboard_paginates_and_ranks(con, sources):
    lb = query.leaderboard(con, "hotosm", sources)
    assert lb["total"] == 2 and lb["page"] == 1 and lb["total_pages"] == 1
    items = lb["items"]
    assert [(r["name"], r["changesets"], r["rank"]) for r in items] == [("alice", 2, 1), ("bob", 1, 2)]
    assert items[0]["nodes_created"] == 15  # user 1: 10 (history) + 5 (recent)
    assert set(items[0]["editors"]) == {"iD"}


def test_leaderboard_page_size_and_sort(con, sources):
    # page_size=1 returns one row per page; total spans both users.
    p1 = query.leaderboard(con, "hotosm", sources, page=1, page_size=1)
    p2 = query.leaderboard(con, "hotosm", sources, page=2, page_size=1)
    assert p1["total"] == 2 and p1["total_pages"] == 2
    assert p1["items"][0]["name"] == "alice" and p1["items"][0]["rank"] == 1
    assert p2["items"][0]["name"] == "bob" and p2["items"][0]["rank"] == 2
    # sort by name ascending, and search narrows total.
    byname = query.leaderboard(con, "hotosm", sources, sort="name", order="asc")
    assert [r["name"] for r in byname["items"]] == ["alice", "bob"]
    found = query.leaderboard(con, "hotosm", sources, q="ali")
    assert found["total"] == 1 and found["items"][0]["name"] == "alice"


def test_leaderboard_includes_per_user_tag_stats(con, sources):
    # The frontend reads per-user `tag_stats` (nested {key: {value: {c, m}}}) to show building/highway
    # per contributor; regression guard that leaderboard rows carry it across the history+recent seam.
    items = query.leaderboard(con, "hotosm", sources)["items"]
    alice = next(r for r in items if r["name"] == "alice")
    assert alice["tag_stats"]["building"]["yes"] == {"c": 5, "m": 2}  # 4 (history) + 1 (recent) creates; 2 modifies
    bob = next(r for r in items if r["name"] == "bob")
    assert bob["tag_stats"] == {}  # user with no tagged changesets


def test_leaderboard_per_user_hashtags(con, sources):
    # The user modal shows each contributor's hashtags; they come from the rollup `hashtag` column
    # (history) and the base changesets `hashtags` list (recent), matching the queried prefix.
    items = query.leaderboard(con, "hotosm", sources)["items"]
    alice = next(r for r in items if r["name"] == "alice")
    assert set(alice["hashtags"]) == {"#hotosm-project-1", "#hotosm-project-2"}  # history + recent
    bob = next(r for r in items if r["name"] == "bob")
    assert bob["hashtags"] == ["#hotosm-project-1"]


def test_trending_hashtags(con, sources):
    # Contributors + edits per matched hashtag: project-1 has alice+bob (history, 10+6 edits), project-2
    # has alice (recent, 5 edits).
    ht = query.hashtags(con, "hotosm", sources)
    assert ht == [
        {"hashtag": "#hotosm-project-1", "users": 2, "edits": 16},
        {"hashtag": "#hotosm-project-2", "users": 1, "edits": 5},
    ]


def test_tags_breakdown(con, sources):
    tg = query.tags(con, "hotosm", sources)
    building = next(r for r in tg if r["tag_key"] == "building")
    assert building["tag_value"] == "yes"
    assert building["creates"] == 5 and building["modifies"] == 2  # 4 (history) + 1 (recent) creates; 2 modifies


def test_exact_hashtag_no_prefix_bleed(con, sources):
    # An exact non-hotosm hashtag returns nothing from this fixture.
    assert query.summary(con, "missingmaps", sources)["changesets"] == 0
