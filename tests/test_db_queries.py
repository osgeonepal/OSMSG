"""Populate a fresh DuckDB with hand-crafted rows and verify query outputs."""

from __future__ import annotations

import pytest

from osmsg.db.ingest import _sql_escape
from osmsg.db.queries import attach_metadata, attach_tag_stats, daily_summary, user_stats


def _tags_literal(nested: dict) -> str:
    """A {key: {value: {c, m[, len]}}} dict as a DuckDB STRUCT(k,v,c,m,len_m)[] literal (test helper)."""
    parts = []
    for key, by_value in nested.items():
        for value, s in by_value.items():
            len_m = "NULL" if s.get("len") is None else repr(s["len"])
            parts.append(f"{{'k':'{key}','v':'{value}','c':{s['c']},'m':{s['m']},'len_m':{len_m}}}")
    return "[" + ", ".join(parts) + "]"


def test_sql_escape_doubles_single_quotes():
    """Path-quote helper must not let single quotes break out of a SQL string literal."""
    assert _sql_escape("a'b") == "a''b"
    assert _sql_escape("no quotes") == "no quotes"
    assert _sql_escape("'") == "''"
    assert _sql_escape("''") == "''''"


@pytest.fixture
def populated_db(fresh_db):
    conn = fresh_db
    conn.execute("INSERT INTO users VALUES (10, 'alice'), (20, 'bob')")
    conn.execute(
        """
        INSERT INTO changesets (changeset_id, uid, created_at, hashtags, editor, geom)
        VALUES
            (1, 10, '2026-04-01 10:00:00+00', ['#hotosm-project-1', '#mapathon'], 'iD',
                ST_MakeEnvelope(85.0, 27.0, 85.5, 27.5)),
            (2, 10, '2026-04-01 14:00:00+00', ['#mapathon'], 'iD', NULL),
            (3, 20, '2026-04-02 09:00:00+00', NULL, 'JOSM', NULL)
        """
    )
    tags_alice = _tags_literal(
        {"building": {"yes": {"c": 5, "m": 1}}, "highway": {"residential": {"c": 2, "m": 0, "len": 120.6}}}
    )
    tags_bob = _tags_literal({"natural": {"tree": {"c": 10, "m": 0}}})
    conn.execute(
        f"""
        INSERT INTO changeset_stats VALUES
            (1, 100, 10, 30, 5, 0, 8, 1, 0, 0, 0, 0, 5, 1, {tags_alice}),
            (2, 101, 10, 10, 0, 0, 4, 0, 0, 0, 0, 0, 0, 0, []),
            (3, 102, 20, 50, 0, 0, 0, 0, 0, 0, 0, 0, 50, 0, {tags_bob})
        """
    )
    return conn


def test_user_stats_aggregates_across_changesets_per_user(populated_db):
    rows = user_stats(populated_db)
    assert len(rows) == 2

    alice = next(r for r in rows if r["name"] == "alice")
    assert alice["uid"] == 10
    assert alice["changesets"] == 2
    assert alice["nodes_created"] == 40  # 30 + 10
    assert alice["ways_created"] == 12  # 8 + 4
    assert alice["poi_created"] == 5
    assert alice["map_changes"] == (
        alice["nodes_created"]
        + alice["nodes_modified"]
        + alice["nodes_deleted"]
        + alice["ways_created"]
        + alice["ways_modified"]
        + alice["ways_deleted"]
        + alice["rels_created"]
        + alice["rels_modified"]
        + alice["rels_deleted"]
    )


def test_user_stats_orders_by_map_changes_desc(populated_db):
    rows = user_stats(populated_db)
    assert [r["name"] for r in rows] == sorted(
        [r["name"] for r in rows], key=lambda n: -next(x for x in rows if x["name"] == n)["map_changes"]
    )
    assert rows[0]["rank"] == 1
    assert rows[-1]["rank"] == len(rows)


def test_user_stats_top_n(populated_db):
    rows = user_stats(populated_db, top_n=1)
    assert len(rows) == 1
    assert rows[0]["rank"] == 1


def test_attach_metadata_pulls_hashtags_and_editors(populated_db):
    rows = user_stats(populated_db)
    attach_metadata(populated_db, rows)
    alice = next(r for r in rows if r["name"] == "alice")
    bob = next(r for r in rows if r["name"] == "bob")
    assert set(alice["hashtags"]) == {"#hotosm-project-1", "#mapathon"}
    assert alice["editors"] == ["iD"]
    assert bob["hashtags"] == []  # bob's changeset had NULL hashtags
    assert bob["editors"] == ["JOSM"]


def test_attach_tag_stats_with_additional_keys(populated_db):
    rows = user_stats(populated_db)
    attach_tag_stats(populated_db, rows, additional_tags=["building", "highway"])
    alice = next(r for r in rows if r["name"] == "alice")
    assert alice["building_create"] == 5
    assert alice["building_modify"] == 1
    assert alice["highway_create"] == 2


def test_attach_tag_stats_with_length(populated_db):
    rows = user_stats(populated_db)
    attach_tag_stats(populated_db, rows, length_tags=["highway"])
    alice = next(r for r in rows if r["name"] == "alice")
    assert alice["highway_len_m"] == 121  # rounded from 120.6


def test_attach_tag_stats_all_tags_with_key_value(populated_db):
    rows = user_stats(populated_db)
    attach_tag_stats(populated_db, rows, tag_mode="all")
    alice = next(r for r in rows if r["name"] == "alice")
    assert alice["tags_create"]["building"] == 5
    assert alice["tags_create"]["building=yes"] == 5
    assert alice["tags_create"]["highway=residential"] == 2


def test_attach_tag_stats_keys_mode_omits_value_breakdown(populated_db):
    rows = user_stats(populated_db)
    attach_tag_stats(populated_db, rows, tag_mode="keys")
    alice = next(r for r in rows if r["name"] == "alice")
    assert alice["tags_create"]["building"] == 5
    assert "building=yes" not in alice["tags_create"]
    assert "highway=residential" not in alice["tags_create"]


def test_daily_summary_groups_by_utc_date(populated_db):
    rows = daily_summary(populated_db)
    by_date = {r["date"]: r for r in rows}
    assert "2026-04-01" in by_date
    assert "2026-04-02" in by_date
    assert by_date["2026-04-01"]["users"] == 1  # alice only
    assert by_date["2026-04-01"]["changesets"] == 2
    assert by_date["2026-04-02"]["users"] == 1  # bob only
