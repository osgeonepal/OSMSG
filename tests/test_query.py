"""The hashtag query surface combines history + recent and computes correct summary/leaderboard/tags."""

from __future__ import annotations

import dataclasses
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
        f"{cols}, tags STRUCT(k VARCHAR, v VARCHAR, c BIGINT, m BIGINT, l DOUBLE)[]"
    )
    zeros = ", ".join(["0"] * len(COUNT_COLS))
    b = "[{'k':'building','v':'yes','c':%d,'m':%d,'l':NULL}]"
    # history is the published rollup (native tags list); recent is the live base tables.
    c.execute(f"CREATE TABLE history ({hist_ddl})")
    c.execute(
        f"""INSERT INTO history VALUES
        ('#hotosm-project-1', 1, 1, 'iD', '2026-05-01', {zeros.replace("0", "10", 1)}, {b % (4, 0)}),
        ('#hotosm-project-1', 2, 2, 'JOSM', '2026-05-02', {zeros.replace("0", "6", 1)}, [])"""
    )
    c.execute(
        f"CREATE TABLE cs_stats (changeset_id BIGINT, seq_id BIGINT, uid BIGINT, {cols}, "
        "tags STRUCT(k VARCHAR, v VARCHAR, c BIGINT, m BIGINT, l DOUBLE)[])"
    )
    c.execute(
        f"""INSERT INTO cs_stats VALUES
        (3, 0, 1, {zeros.replace("0", "5", 1)}, [{{'k':'building','v':'yes','c':1,'m':2,'l':NULL}}])"""
    )
    c.execute(
        "CREATE TABLE csets (changeset_id BIGINT, uid BIGINT, editor VARCHAR, created_at TIMESTAMP, hashtags VARCHAR[])"
    )
    c.execute("INSERT INTO csets VALUES (3, 1, 'iD', '2026-07-05', ['#hotosm-project-2', '#waterproject'])")
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


def test_leaderboard_search_escapes_like_wildcards(con, sources):
    # `_` and `%` are LIKE metacharacters: unescaped, `a_ice` matched `alice` and a bare `%` returned
    # every contributor. They must match literally.
    assert query.leaderboard(con, "hotosm", sources, q="ali")["total"] == 1
    assert query.leaderboard(con, "hotosm", sources, q="a_ice")["total"] == 0
    assert query.leaderboard(con, "hotosm", sources, q="%")["total"] == 0


def test_leaderboard_includes_per_user_tag_stats(con, sources):
    # The frontend reads per-user `tag_stats` (nested {key: {value: {c, m}}}) to show building/highway
    # per contributor; regression guard that leaderboard rows carry it across the history+recent seam.
    items = query.leaderboard(con, "hotosm", sources)["items"]
    alice = next(r for r in items if r["name"] == "alice")
    assert alice["tag_stats"]["building"]["yes"] == {"c": 5, "m": 2}  # 4 (history) + 1 (recent) creates; 2 modifies
    bob = next(r for r in items if r["name"] == "bob")
    assert bob["tag_stats"] == {}  # user with no tagged changesets


def test_leaderboard_per_user_recent_hashtags(con, sources):
    # The user modal shows each contributor's RECENT co-occurring hashtags only (the live tail): alice's
    # recent changeset carries #hotosm-project-2 and the co-tagged #waterproject; bob has no recent activity.
    items = query.leaderboard(con, "hotosm", sources)["items"]
    alice = next(r for r in items if r["name"] == "alice")
    assert set(alice["hashtags"]) == {"#hotosm-project-2", "#waterproject"}
    bob = next(r for r in items if r["name"] == "bob")
    assert bob["hashtags"] == []


def test_cooccurring_hashtags(con, sources):
    # Hashtags carried by the matched changesets (co-occurring), not just the searched ones: project-1
    # from history (alice+bob), and on the recent changeset that matched via project-2 the panel also
    # surfaces the co-tagged #waterproject.
    ht = query.hashtags(con, "hotosm", sources)
    assert ht == [
        {"hashtag": "#hotosm-project-1", "users": 2, "edits": 16},
        {"hashtag": "#hotosm-project-2", "users": 1, "edits": 5},
        {"hashtag": "#waterproject", "users": 1, "edits": 5},
    ]


def test_global_window_stats_and_consistency(con, sources):
    # Whole-OSM stats over a window, no hashtag. Add two more recent changesets (bob JOSM, carol iD) so the
    # aggregation spans several users/editors; the existing recent changeset 3 is alice (iD).
    con.execute("INSERT INTO users VALUES (3, 'carol')")
    con.execute(
        "INSERT INTO cs_stats VALUES "
        "(10, 0, 2, 3,0,0,0,0,0,0,0,0,0,0, []), "  # bob: nodes_created=3
        "(11, 0, 3, 0,0,0,2,0,0,0,0,0,0,0, [])"  # carol: ways_created=2
    )
    con.execute("INSERT INTO csets VALUES (10, 2, 'JOSM', '2026-07-06', ['#foo']), (11, 3, 'iD', '2026-07-07', [])")
    start, end = dt.datetime(2026, 7, 1, tzinfo=dt.UTC), dt.datetime(2026, 8, 1, tzinfo=dt.UTC)

    summ = query.global_summary(con, sources, start=start, end=end)
    assert summ["users"] == 3 and summ["changesets"] == 3
    assert summ["map_changes"] == 10  # alice 5 + bob 3 + carol 2

    lb = query.global_leaderboard(con, sources, start=start, end=end)
    assert lb["total"] == 3  # consistency: leaderboard total equals summary users
    assert [(r["name"], r["map_changes"], r["rank"]) for r in lb["items"]] == [
        ("alice", 5, 1),
        ("bob", 3, 2),
        ("carol", 2, 3),
    ]

    eds = {e["editor"]: e for e in query.global_editors(con, sources, start=start, end=end)}
    assert eds["iD"]["changesets"] == 2 and eds["iD"]["users"] == 2  # alice + carol
    assert eds["JOSM"]["changesets"] == 1 and eds["JOSM"]["users"] == 1

    tr = {t["hashtag"]: t for t in query.global_trending(con, sources, start=start, end=end)}
    assert set(tr) == {"#hotosm-project-2", "#waterproject", "#foo"}
    assert tr["#foo"]["users"] == 1

    # Per-user attaches on the leaderboard rows, full parity with the hashtag leaderboard.
    alice = next(r for r in lb["items"] if r["name"] == "alice")
    assert alice["tag_stats"]["building"]["yes"] == {"c": 1, "m": 2}
    assert alice["editors"] == ["iD"]
    assert set(alice["hashtags"]) == {"#hotosm-project-2", "#waterproject"}
    bob = next(r for r in lb["items"] if r["name"] == "bob")
    assert bob["tag_stats"] == {} and bob["editors"] == ["JOSM"] and bob["hashtags"] == ["#foo"]


def test_global_leaderboard_gates_heavy_page_tag_stats(con, sources, monkeypatch):
    # Over-threshold page skips per-user tags; editors/hashtags still attach.
    start, end = dt.datetime(2026, 7, 1, tzinfo=dt.UTC), dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    monkeypatch.setattr(query, "_MAX_GLOBAL_TAG_MAP_CHANGES", 0)
    lb = query.global_leaderboard(con, sources, start=start, end=end)
    assert lb["total"] >= 1
    assert all(r["tag_stats"] == {} for r in lb["items"])  # gated off
    alice = next(r for r in lb["items"] if r["name"] == "alice")
    assert alice["editors"] == ["iD"] and set(alice["hashtags"]) == {"#hotosm-project-2", "#waterproject"}

    # Aggregate global tag breakdown (the detailed tag stats panel).
    gt = query.global_tags(con, sources, start=start, end=end)
    assert len(gt) == 1 and gt[0]["tag_key"] == "building" and gt[0]["tag_value"] == "yes"
    assert gt[0]["creates"] == 1 and gt[0]["modifies"] == 2

    # A window before the data returns nothing (no rows, zero users).
    empty = query.global_summary(con, sources, start=dt.datetime(2020, 1, 1, tzinfo=dt.UTC), end=start)
    assert empty["users"] == 0 and empty["changesets"] == 0


def test_all_time_warm_fills_caches_and_is_idempotent(con, sources, tmp_path):
    s = dataclasses.replace(sources, cache_dir=str(tmp_path))
    assert query.all_time_warm_pending(s, "hotosm") is True
    query.warm_all_time(con, "hotosm", s)
    assert query.all_time_warm_pending(s, "hotosm") is False
    # The co-occurring cache is always written; the per-user tags cache is skipped for this non-mega
    # hashtag (its tags serve inline, so the cache would never be read).
    assert list(tmp_path.glob("cooccur-*.parquet"))
    assert not list(tmp_path.glob("lb_tags-*.parquet"))
    # A second warm is a no-op (cache already present) and does not raise.
    query.warm_all_time(con, "hotosm", s)
    # Without a cache dir there is nothing to warm.
    assert query.all_time_warm_pending(sources, "hotosm") is False


def test_trending_from_cooccur_cache_matches_live(con, sources, tmp_path):
    # The warmed co-occurring cache serves the same all-time trending result as the live self-join.
    s = dataclasses.replace(sources, cache_dir=str(tmp_path))
    query.warm_all_time(con, "hotosm", s)
    assert query.hashtags(con, "hotosm", s) == query.hashtags(con, "hotosm", sources)


def test_mega_leaderboard_tags_served_from_cache(con, sources, tmp_path, monkeypatch):
    # Force the mega path (history rows over the gate): without a cache the per-user tags are gated off;
    # after a warm the leaderboard serves them from the cache plus the live recent tail.
    monkeypatch.setattr(query, "_MAX_TAG_ROWS", 1)
    s = dataclasses.replace(sources, cache_dir=str(tmp_path))
    query.warm_all_time(con, "hotosm", s)
    gated = query.leaderboard(con, "hotosm", sources)
    assert gated["tags_gated"] is True and all(r["tag_stats"] == {} for r in gated["items"])
    served = query.leaderboard(con, "hotosm", s)
    assert served["tags_gated"] is False
    alice = next(r for r in served["items"] if r["name"] == "alice")
    assert alice["tag_stats"]["building"]["yes"] == {"c": 5, "m": 2}  # 4 history (cache) + 1 recent


def _tag_written_both_ways(con) -> None:
    """The same hashtag as the two sides really store it: the rollup lowercases, while the base
    `changesets` table keeps the case the mapper typed."""
    con.execute("INSERT INTO history SELECT '#youthmappers', * EXCLUDE (hashtag) FROM history WHERE changeset_id = 1")
    con.execute("UPDATE csets SET hashtags = list_append(hashtags, '#YouthMappers') WHERE changeset_id = 3")


def test_cooccurring_hashtags_merge_case_variants(con, sources):
    # Grouping is on the raw string, so a tag written both ways used to come back as two rows, each
    # holding only part of the contributors. Both sides lowercase before grouping.
    _tag_written_both_ways(con)
    tags = [r["hashtag"] for r in query.hashtags(con, "hotosm", sources)]
    assert "#YouthMappers" not in tags
    assert tags.count("#youthmappers") == 1


def test_leaderboard_cooccurring_hashtags_merge_case_variants(con, sources):
    # Same split on the per-user chips: alice carries the tag from history and from the recent tail.
    _tag_written_both_ways(con)
    items = query.leaderboard(con, "hotosm", sources)["items"]
    alice = next(r for r in items if r["name"] == "alice")
    assert "#YouthMappers" not in alice["hashtags"]
    assert alice["hashtags"].count("#youthmappers") == 1


def test_tags_breakdown(con, sources):
    tg = query.tags(con, "hotosm", sources)
    building = next(r for r in tg if r["tag_key"] == "building")
    assert building["tag_value"] == "yes"
    assert building["creates"] == 5 and building["modifies"] == 2  # 4 (history) + 1 (recent) creates; 2 modifies


def test_exact_hashtag_no_prefix_bleed(con, sources):
    # An exact non-hotosm hashtag returns nothing from this fixture.
    assert query.summary(con, "missingmaps", sources)["changesets"] == 0


def test_exact_match_excludes_longer_hashtags(con, sources):
    """A longer hashtag sharing the search as a prefix (#hotosm-project-11 vs #hotosm-project-1) is counted
    under prefix search but excluded under exact search."""
    zeros = ", ".join(["0"] * len(COUNT_COLS))
    con.execute(
        f"INSERT INTO history VALUES ('#hotosm-project-11', 10, 3, 'iD', '2026-05-03', "
        f"{zeros.replace('0', '4', 1)}, [])"
    )
    prefix = query.summary(con, "hotosm-project-1", sources)
    assert prefix["changesets"] == 3  # #hotosm-project-1 (cs 1, 2) + #hotosm-project-11 (cs 10)
    exact = query.summary(con, "hotosm-project-1", sources, exact=True)
    assert exact["changesets"] == 2  # only the two #hotosm-project-1 changesets


def test_cache_hit_equals_miss(con, sources, tmp_path):
    """The all-time cache is pure memoization: miss (computes + writes) and hit (reads) both equal the
    uncached result. This is the guarantee that caching never changes the numbers."""
    baseline = query.summary(con, "hotosm", sources)
    cached = dataclasses.replace(sources, cache_dir=str(tmp_path))
    miss = query.summary(con, "hotosm", cached)
    assert len(list(tmp_path.glob("summary_users-*.parquet"))) == 1
    hit = query.summary(con, "hotosm", cached)
    assert baseline == miss == hit


def test_cache_skips_windowed_queries(con, sources, tmp_path):
    cached = dataclasses.replace(sources, cache_dir=str(tmp_path))
    query.summary(con, "hotosm", cached, start=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    assert list(tmp_path.glob("*.parquet")) == []


def test_cache_frontier_advance_uses_new_file(con, sources, tmp_path):
    cached = dataclasses.replace(sources, cache_dir=str(tmp_path))
    query.summary(con, "hotosm", cached)
    query.summary(con, "hotosm", dataclasses.replace(cached, frontier=dt.datetime(2026, 8, 1, tzinfo=dt.UTC)))
    assert len(list(tmp_path.glob("summary_users-*.parquet"))) == 2


def test_recent_tail_cache_noop_without_postgres(con, sources, tmp_path):
    """The recent-tail slice cache only engages for an all-time query over attached Postgres with a cache
    dir; otherwise the sources are returned unchanged so the recent side stays live."""
    prefixes = query._prefixes("hotosm")
    # No pg_attach -> unchanged even with a cache dir.
    local = dataclasses.replace(sources, cache_dir=str(tmp_path))
    assert query._with_recent_tail_cache(con, local, prefixes, None, None) is local
    # pg_attach + cache dir but a window -> unchanged (recent stays live for windowed queries).
    pg = dataclasses.replace(sources, cache_dir=str(tmp_path), pg_attach="pg")
    windowed = query._with_recent_tail_cache(con, pg, prefixes, dt.datetime(2026, 1, 1, tzinfo=dt.UTC), None)
    assert windowed is pg
    assert list(tmp_path.glob("recent_*.parquet")) == []
