"""Unit tests for pydantic models + flatten-to-row contract."""

import datetime as dt

from osmsg.models import Action, Changeset, ChangesetStats, ElementStat, TagValueStat, User


def test_action_is_strenum():
    assert Action.CREATE == "create"
    assert Action.MODIFY == "modify"
    assert Action.DELETE == "delete"


def test_element_stat_add_increments_correct_bucket():
    s = ElementStat()
    s.add(Action.CREATE)
    s.add(Action.CREATE)
    s.add(Action.MODIFY)
    s.add(Action.DELETE)
    assert (s.c, s.m, s.d) == (2, 1, 1)
    assert s.total == 4


def test_tag_value_stat_add_action_only_tracks_create_and_modify():
    tv = TagValueStat()
    tv.add(Action.CREATE)
    tv.add(Action.MODIFY)
    tv.add(Action.DELETE)  # ignored
    assert (tv.c, tv.m) == (1, 1)


def test_tag_value_stat_add_length_starts_at_zero():
    tv = TagValueStat()
    assert tv.len is None
    tv.add_length(120.5)
    tv.add_length(80.0)
    assert tv.len == 200.5


def test_changeset_stats_map_changes_sums_all_buckets():
    s = ChangesetStats(changeset_id=1, uid=1, seq_id=1)
    s.nodes.c, s.nodes.m, s.nodes.d = 3, 2, 1
    s.ways.c, s.ways.m, s.ways.d = 5, 0, 0
    s.rels.c = 1
    assert s.map_changes == 12


def test_changeset_stats_to_row_flattens_buckets_and_emits_native_tags():
    s = ChangesetStats(changeset_id=42, uid=7, seq_id=99)
    s.nodes.c = 5
    s.ways.m = 2
    s.poi_created = 3
    s.tag_stats["building"] = {"yes": TagValueStat(c=2, m=1)}
    s.tag_stats["highway"] = {"residential": TagValueStat(c=1, len=120.0)}

    row = s.to_row()
    assert row[0] == 42  # changeset_id
    assert row[1] == 99  # seq_id
    assert row[2] == 7  # uid
    assert row[3] == 5  # nodes_created
    assert row[7] == 2  # ways_modified
    assert row[12] == 3  # poi_created

    # The shard column is a native list of {k, v, c, m, l} rows, not a JSON string.
    assert row[14] == [
        {"k": "building", "v": "yes", "c": 2, "m": 1, "l": None},
        {"k": "highway", "v": "residential", "c": 1, "m": 0, "l": 120.0},
    ]


def test_changeset_stats_to_row_emits_empty_tags_when_none():
    s = ChangesetStats(changeset_id=1, uid=1, seq_id=1)
    assert s.to_row()[14] == []


def test_user_to_row_pair():
    assert User(uid=10, username="alice").to_row() == (10, "alice")


def test_changeset_to_row_explodes_bbox_into_four_floats():
    cs = Changeset(
        changeset_id=1,
        uid=10,
        created_at=dt.datetime(2026, 4, 1, tzinfo=dt.UTC),
        hashtags=["#test"],
        editor="iD",
        bbox=(85.0, 27.0, 86.0, 28.0),
    )
    row = cs.to_row()
    assert row[0] == 1
    assert row[1] == 10
    assert row[3] == ["#test"]
    assert row[4] == "iD"
    assert row[5:9] == (85.0, 27.0, 86.0, 28.0)


def test_changeset_to_row_handles_missing_bbox():
    cs = Changeset(changeset_id=1, uid=10)
    assert cs.to_row()[5:9] == (None, None, None, None)
