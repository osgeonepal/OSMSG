"""Process synthetic .osc files end-to-end through the handler and verify counts."""

from __future__ import annotations

import datetime as dt

import pytest

from osmsg.handlers import ChangefileHandler, ChangesetHandler


def _write_changeset_xml(tmp_path, name, changesets):
    """Hand-written changeset XML, osmium's SimpleWriter doesn't support changesets.

    Pass ``open=True`` to emit a still-open entry (no ``closed_at`` attribute), that
    matches what OSM replication serializes for changesets that haven't closed yet,
    and is what osmium turns into ``closed_at = 1970-01-01`` (the epoch sentinel).
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<osm version="0.6">',
    ]
    for cs in changesets:
        is_open = cs.get("open", False)
        attrs = (
            f'id="{cs["id"]}" created_at="{cs.get("created_at", "2026-04-27T20:00:00Z")}" '
            f'num_changes="{cs.get("num_changes", 1)}" user="{cs.get("user", "alice")}" '
            f'uid="{cs.get("uid", 10)}" comments_count="0"'
        )
        if "bbox" in cs:
            min_lon, min_lat, max_lon, max_lat = cs["bbox"]
            attrs += f' min_lat="{min_lat}" min_lon="{min_lon}" max_lat="{max_lat}" max_lon="{max_lon}"'
        if is_open:
            attrs += ' open="true"'
        else:
            attrs = f'{attrs} closed_at="{cs.get("closed_at", "2026-04-27T21:00:00Z")}" open="false"'
        parts.append(f"  <changeset {attrs}>")
        for k, v in cs.get("tags", {}).items():
            parts.append(f'    <tag k="{k}" v="{v}"/>')
        parts.append("  </changeset>")
    parts.append("</osm>")
    p = tmp_path / name
    p.write_text("\n".join(parts), encoding="utf-8")
    return p


@pytest.fixture
def changeset_config():
    return {
        "hashtags": None,
        "exact_lookup": False,
        "changeset_meta": True,
        "whitelisted_users": [],
        "geom_filter_wkt": None,
    }


def test_changeset_handler_matches_hashtag_in_comment(tmp_path, changeset_config):
    changeset_config["hashtags"] = ["#hotosm"]
    p = _write_changeset_xml(
        tmp_path,
        "cs_comment.osm",
        [{"id": 1, "tags": {"comment": "Mapping #hotosm-project-99 buildings"}}],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert 1 in h.changesets
    assert h.changesets[1].hashtags == ["#hotosm-project-99"]


def test_changeset_handler_matches_hashtag_in_hashtags_field(tmp_path, changeset_config):
    """Regression: editors that only fill the `hashtags` tag (e.g. comment='buildings'
    with hashtags='#hotosm-project-99;#GEOSM') used to be silently dropped."""
    changeset_config["hashtags"] = ["#hotosm"]
    p = _write_changeset_xml(
        tmp_path,
        "cs_field.osm",
        [{"id": 1, "tags": {"comment": "buildings", "hashtags": "#hotosm-project-99;#GEOSM"}}],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert 1 in h.changesets
    assert "#hotosm-project-99" in h.changesets[1].hashtags
    assert "#GEOSM" in h.changesets[1].hashtags


def test_changeset_handler_exact_lookup_uses_hashtags_field(tmp_path, changeset_config):
    """--exact-lookup must also examine the explicit `hashtags` field tokens."""
    changeset_config["hashtags"] = ["#GEOSM"]
    changeset_config["exact_lookup"] = True
    p = _write_changeset_xml(
        tmp_path,
        "cs_exact.osm",
        [
            {"id": 1, "tags": {"comment": "buildings", "hashtags": "#GEOSM;#hotosm-project-99"}},
            # No exact match: comment has '#GEOSMfoo' (no whole-word #GEOSM), hashtags absent.
            {"id": 2, "tags": {"comment": "buildings #GEOSMfoo"}},
        ],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert set(h.changesets.keys()) == {1}


def test_changeset_handler_drops_when_neither_field_matches(tmp_path, changeset_config):
    changeset_config["hashtags"] = ["#hotosm"]
    p = _write_changeset_xml(
        tmp_path,
        "cs_nomatch.osm",
        [{"id": 1, "tags": {"comment": "buildings", "hashtags": "#OzonGeo"}}],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert h.changesets == {}


def test_changeset_handler_handles_no_comment_no_hashtags(tmp_path, changeset_config):
    """A changeset with no comment and no hashtags must not crash and must not match a filter."""
    changeset_config["hashtags"] = ["#hotosm"]
    p = _write_changeset_xml(tmp_path, "cs_bare.osm", [{"id": 1, "tags": {}}])
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert h.changesets == {}


def test_changeset_handler_handles_empty_tag_values(tmp_path, changeset_config):
    """Empty `comment` and empty `hashtags` values must not match a non-empty filter."""
    changeset_config["hashtags"] = ["#hotosm"]
    p = _write_changeset_xml(
        tmp_path,
        "cs_empty.osm",
        [{"id": 1, "tags": {"comment": "", "hashtags": ""}}],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert h.changesets == {}


@pytest.mark.parametrize(
    "field_value,expected_tokens",
    [
        ("#hotosm;#map", ["#hotosm", "#map"]),  # canonical
        ("  ;  #hotosm  ;;  #map  ", ["#hotosm", "#map"]),  # whitespace + empty splits
        ("#hotosm #map", ["#hotosm", "#map"]),  # space-separated (real-world)
        ("#hotosm,#map", ["#hotosm", "#map"]),  # comma-separated (real-world)
    ],
)
def test_changeset_handler_tokenizes_hashtags_field_robustly(tmp_path, changeset_config, field_value, expected_tokens):
    """The `hashtags` field is canonically `;`-separated, but real data also uses
    spaces and commas. Tokenization must extract `#word` regardless of separator
    so we don't store malformed tokens like `'#hotosm #map'` or miss matches."""
    changeset_config["hashtags"] = ["#hotosm"]
    p = _write_changeset_xml(
        tmp_path,
        "cs_seps.osm",
        [{"id": 1, "tags": {"comment": "x", "hashtags": field_value}}],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert 1 in h.changesets
    assert h.changesets[1].hashtags == expected_tokens


def test_changeset_handler_dedup_case_insensitive_preserves_first(tmp_path, changeset_config):
    """Same hashtag in both fields: stored once, with the case that appeared first (comment)."""
    changeset_config["hashtags"] = ["#hotosm"]
    p = _write_changeset_xml(
        tmp_path,
        "cs_dup.osm",
        [{"id": 1, "tags": {"comment": "Mapping #HotOSM today", "hashtags": "#hotosm"}}],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert h.changesets[1].hashtags == ["#HotOSM"]


def test_changeset_handler_substring_matches_partial_token(tmp_path, changeset_config):
    """`--hashtags hotosm` (substring) must match `#hotosm-project-99`."""
    changeset_config["hashtags"] = ["#hotosm"]
    p = _write_changeset_xml(
        tmp_path,
        "cs_partial.osm",
        [{"id": 1, "tags": {"comment": "x", "hashtags": "#hotosm-project-99"}}],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert 1 in h.changesets


def test_changeset_handler_exact_lookup_rejects_partial_token(tmp_path, changeset_config):
    """`--exact-lookup` must NOT match `#hotosmgermany` when filtering on `#hotosm`."""
    changeset_config["hashtags"] = ["#hotosm"]
    changeset_config["exact_lookup"] = True
    p = _write_changeset_xml(
        tmp_path,
        "cs_partial_exact.osm",
        [
            {"id": 1, "tags": {"comment": "x", "hashtags": "#hotosmgermany"}},  # NOT a match
            {"id": 2, "tags": {"comment": "x", "hashtags": "#hotosm"}},  # exact match
        ],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert set(h.changesets.keys()) == {2}


def test_changeset_handler_filter_is_case_insensitive(tmp_path, changeset_config):
    """Users pass `--hashtags HOTOSM` or `--hashtags hotosm` interchangeably."""
    changeset_config["hashtags"] = ["#HOTOSM"]
    p = _write_changeset_xml(
        tmp_path,
        "cs_case.osm",
        [{"id": 1, "tags": {"comment": "x", "hashtags": "#hotosm-project-99"}}],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert 1 in h.changesets


def test_changeset_handler_changeset_meta_collects_all_when_no_hashtags_filter(tmp_path, changeset_config):
    """`--changeset` with no hashtag filter: every changeset is recorded."""
    changeset_config["hashtags"] = None
    p = _write_changeset_xml(
        tmp_path,
        "cs_all.osm",
        [
            {"id": 1, "tags": {"comment": "buildings"}},
            {"id": 2, "tags": {}},
            {"id": 3, "tags": {"hashtags": "#anything"}},
        ],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert set(h.changesets.keys()) == {1, 2, 3}


def test_changeset_handler_user_whitelist_intersects_hashtag_filter(tmp_path, changeset_config):
    """`--hashtags` AND `--users`: both must pass for the changeset to be kept."""
    changeset_config["hashtags"] = ["#hotosm"]
    changeset_config["whitelisted_users"] = ["alice"]
    p = _write_changeset_xml(
        tmp_path,
        "cs_whitelist.osm",
        [
            {"id": 1, "user": "alice", "uid": 10, "tags": {"hashtags": "#hotosm-foo"}},
            {"id": 2, "user": "bob", "uid": 20, "tags": {"hashtags": "#hotosm-foo"}},
            {"id": 3, "user": "alice", "uid": 10, "tags": {"hashtags": "#unrelated"}},
        ],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert set(h.changesets.keys()) == {1}


def test_changeset_handler_stores_editor_and_bbox(tmp_path, changeset_config):
    """`created_by` becomes `editor`; min_*/max_* become bbox."""
    changeset_config["hashtags"] = ["#hotosm"]
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<osm version="0.6">',
        '  <changeset id="1" created_at="2026-04-27T20:00:00Z" closed_at="2026-04-27T21:00:00Z" '
        'open="false" num_changes="1" user="alice" uid="10" comments_count="0" '
        'min_lat="10.5" max_lat="11.5" min_lon="20.5" max_lon="21.5">',
        '    <tag k="comment" v="x"/>',
        '    <tag k="hashtags" v="#hotosm-project-99"/>',
        '    <tag k="created_by" v="JOSM/1.5"/>',
        "  </changeset>",
        "</osm>",
    ]
    p = tmp_path / "cs_editor.osm"
    p.write_text("\n".join(parts), encoding="utf-8")
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    cs = h.changesets[1]
    assert cs.editor == "JOSM/1.5"
    assert cs.bbox == (20.5, 10.5, 21.5, 11.5)
    assert cs.uid == 10


def test_changeset_handler_first_seen_wins_on_duplicate_ids(tmp_path, changeset_config):
    """Same changeset id appearing twice (e.g. across two replication files) is recorded once."""
    changeset_config["hashtags"] = ["#hotosm"]
    p = _write_changeset_xml(
        tmp_path,
        "cs_dup_ids.osm",
        [
            {"id": 1, "tags": {"hashtags": "#hotosm-foo"}, "user": "alice", "uid": 10},
            {"id": 1, "tags": {"hashtags": "#hotosm-bar"}, "user": "alice", "uid": 10},
        ],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert h.changesets[1].hashtags == ["#hotosm-foo"]


def test_changeset_handler_window_filter_drops_only_pre_window_closures(tmp_path, changeset_config):
    """Backward-pad changesets that closed entirely before window_start are dropped (they'd
    leak hashtags onto in-window users via attach_metadata). Everything else, including
    changesets created past window_end, is kept so the next tick (which resumes at
    last_seq+1) doesn't have to refetch them; otherwise they'd remain permanent stubs."""
    changeset_config["hashtags"] = ["#hotosm"]
    changeset_config["window_start_utc"] = dt.datetime(2026, 4, 27, 21, 4, tzinfo=dt.UTC)
    changeset_config["window_end_utc"] = dt.datetime(2026, 4, 27, 21, 54, tzinfo=dt.UTC)

    p = _write_changeset_xml(
        tmp_path,
        "cs_window.osm",
        [
            # closed BEFORE window → drop
            {
                "id": 1,
                "created_at": "2026-04-27T20:00:00Z",
                "closed_at": "2026-04-27T20:30:00Z",
                "tags": {"hashtags": "#hotosm-pre"},
            },
            # closed AT window start → drop (closed_at <= start)
            {
                "id": 2,
                "created_at": "2026-04-27T20:30:00Z",
                "closed_at": "2026-04-27T21:04:00Z",
                "tags": {"hashtags": "#hotosm-edge1"},
            },
            # spans window start → KEEP
            {
                "id": 3,
                "created_at": "2026-04-27T20:30:00Z",
                "closed_at": "2026-04-27T21:30:00Z",
                "tags": {"hashtags": "#hotosm-spans-start"},
            },
            # entirely within window → KEEP
            {
                "id": 4,
                "created_at": "2026-04-27T21:10:00Z",
                "closed_at": "2026-04-27T21:20:00Z",
                "tags": {"hashtags": "#hotosm-inside"},
            },
            # spans window end → KEEP
            {
                "id": 5,
                "created_at": "2026-04-27T21:50:00Z",
                "closed_at": "2026-04-27T22:10:00Z",
                "tags": {"hashtags": "#hotosm-spans-end"},
            },
            # created AT window end → KEEP (no `created >= end` filter anymore)
            {
                "id": 6,
                "created_at": "2026-04-27T21:54:00Z",
                "closed_at": "2026-04-27T22:10:00Z",
                "tags": {"hashtags": "#hotosm-edge2"},
            },
            # created+closed entirely after window → KEEP. The next --update tick resumes
            # past this changeset's events; dropping it here would leave it a permanent
            # stub in changeset_stats. Bleed bound: at most one diff past end_date.
            {
                "id": 7,
                "created_at": "2026-04-27T22:10:00Z",
                "closed_at": "2026-04-27T22:30:00Z",
                "tags": {"hashtags": "#hotosm-post"},
            },
        ],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert set(h.changesets.keys()) == {3, 4, 5, 6, 7}


def test_changeset_handler_window_filter_keeps_still_open_changesets(tmp_path, changeset_config):
    """Still-open changesets land regardless of when they were opened: the `c.open` gate
    skips the `closed_at <= start` check (osmium reports 1970 for the sentinel), and no
    upper-bound on created_at is applied (would otherwise create permanent stubs)."""
    changeset_config["hashtags"] = ["#hotosm"]
    changeset_config["window_start_utc"] = dt.datetime(2026, 4, 27, 21, 4, tzinfo=dt.UTC)
    changeset_config["window_end_utc"] = dt.datetime(2026, 4, 27, 21, 54, tzinfo=dt.UTC)

    p = _write_changeset_xml(
        tmp_path,
        "cs_open.osm",
        [
            {"id": 1, "created_at": "2026-04-27T21:50:00Z", "open": True, "tags": {"hashtags": "#hotosm-still-open"}},
            {"id": 2, "created_at": "2026-04-27T21:00:00Z", "open": True, "tags": {"hashtags": "#hotosm-pre-open"}},
            # Created past window_end, still open: KEEP so the next tick (which resumes
            # past this diff) doesn't have to refetch it later.
            {"id": 3, "created_at": "2026-04-27T22:00:00Z", "open": True, "tags": {"hashtags": "#hotosm-post-open"}},
        ],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert set(h.changesets.keys()) == {1, 2, 3}


def test_changeset_handler_window_filter_absent_keeps_everything(tmp_path, changeset_config):
    """No window_start_utc/window_end_utc → no time filter (e.g. unit-test fixtures)."""
    changeset_config["hashtags"] = ["#hotosm"]
    p = _write_changeset_xml(
        tmp_path,
        "cs_no_window.osm",
        [
            {
                "id": 1,
                "created_at": "1999-01-01T00:00:00Z",
                "closed_at": "1999-01-01T00:01:00Z",
                "tags": {"hashtags": "#hotosm-foo"},
            }
        ],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert 1 in h.changesets


def test_changeset_handler_multiple_filters_or_logic(tmp_path, changeset_config):
    """Multiple `--hashtags` values: a changeset matching ANY of them is kept."""
    changeset_config["hashtags"] = ["#hotosm", "#mapathon"]
    p = _write_changeset_xml(
        tmp_path,
        "cs_multi.osm",
        [
            {"id": 1, "tags": {"hashtags": "#hotosm-foo"}},
            {"id": 2, "tags": {"comment": "Weekend #mapathon"}},
            {"id": 3, "tags": {"hashtags": "#unrelated"}},
        ],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert set(h.changesets.keys()) == {1, 2}


def test_changefile_handler_counts_node_create_and_poi(osc_factory, changefile_config):
    """A v1 node with a tag → poi_created=1, nodes.c=1."""
    osc = osc_factory(
        "001.osc",
        [
            (
                "node",
                {"id": 100, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {"amenity": "cafe"}},
            ),
            (
                "node",
                {"id": 101, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {"amenity": "bench"}},
            ),
            # untagged → counts as node create but NOT poi
            ("node", {"id": 102, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {}}),
        ],
    )

    handler = ChangefileHandler(changefile_config, sequence_id=1)
    handler.apply_file(str(osc))

    assert 1 in handler.stats
    s = handler.stats[1]
    assert s.nodes.c == 3
    assert s.nodes.m == 0
    assert s.nodes.d == 0
    assert s.poi_created == 2  # only the tagged ones
    assert s.poi_modified == 0


def test_changefile_handler_counts_way_modify_and_delete(osc_factory, changefile_config):
    osc = osc_factory(
        "002.osc",
        [
            # version=2 → modify
            (
                "way",
                {
                    "id": 1,
                    "version": 2,
                    "uid": 10,
                    "user": "alice",
                    "changeset": 5,
                    "nodes": [1, 2, 3],
                    "tags": {"highway": "residential"},
                },
            ),
            # version=0 means osmium signals deleted → DELETE
        ],
    )
    handler = ChangefileHandler(changefile_config, sequence_id=1)
    handler.apply_file(str(osc))

    s = handler.stats[5]
    assert s.ways.m == 1
    assert s.ways.c == 0


def test_changefile_handler_drops_data_outside_window(osc_factory, changefile_config):
    """Elements with timestamps outside [start_date_utc, end_date_utc) are ignored."""
    changefile_config["start_date_utc"] = dt.datetime(2026, 4, 1, tzinfo=dt.UTC)
    changefile_config["end_date_utc"] = dt.datetime(2026, 4, 2, tzinfo=dt.UTC)

    # Without an explicit timestamp, osmium's apply_file uses 1970, outside our window.
    osc = osc_factory(
        "003.osc",
        [("node", {"id": 1, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {"amenity": "cafe"}})],
    )
    handler = ChangefileHandler(changefile_config, sequence_id=1)
    handler.apply_file(str(osc))
    assert handler.stats == {}


def test_changefile_handler_tracks_specified_tag_keys(osc_factory, changefile_config):
    """--tags building highway → only those keys end up in tag_stats."""
    osc = osc_factory(
        "004.osc",
        [
            (
                "way",
                {
                    "id": 1,
                    "version": 1,
                    "uid": 10,
                    "user": "alice",
                    "changeset": 1,
                    "nodes": [1, 2],
                    "tags": {"building": "yes"},
                },
            ),
            (
                "way",
                {
                    "id": 2,
                    "version": 1,
                    "uid": 10,
                    "user": "alice",
                    "changeset": 1,
                    "nodes": [3, 4],
                    "tags": {"highway": "footway"},
                },
            ),
            # not in --tags list, must NOT appear in tag_stats
            (
                "way",
                {
                    "id": 3,
                    "version": 1,
                    "uid": 10,
                    "user": "alice",
                    "changeset": 1,
                    "nodes": [5, 6],
                    "tags": {"natural": "tree"},
                },
            ),
        ],
    )
    handler = ChangefileHandler(changefile_config, sequence_id=1)
    handler.apply_file(str(osc))

    s = handler.stats[1]
    assert "building" in s.tag_stats
    assert "highway" in s.tag_stats
    assert "natural" not in s.tag_stats
    assert s.tag_stats["building"]["yes"].c == 1
    assert s.tag_stats["highway"]["footway"].c == 1


def test_changefile_handler_all_tags_captures_everything(osc_factory, changefile_config):
    changefile_config["tag_mode"] = "all"
    changefile_config["additional_tags"] = None
    osc = osc_factory(
        "005.osc",
        [
            (
                "node",
                {
                    "id": 1,
                    "version": 1,
                    "uid": 10,
                    "user": "alice",
                    "changeset": 1,
                    "tags": {"amenity": "cafe", "name": "X"},
                },
            ),
        ],
    )
    handler = ChangefileHandler(changefile_config, sequence_id=1)
    handler.apply_file(str(osc))
    assert set(handler.stats[1].tag_stats.keys()) == {"amenity", "name"}


def test_changefile_handler_whitelisted_users_filter(osc_factory, changefile_config):
    """--users alice → only her edits are recorded."""
    changefile_config["whitelisted_users"] = ["alice"]
    osc = osc_factory(
        "006.osc",
        [
            ("node", {"id": 1, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {"amenity": "cafe"}}),
            ("node", {"id": 2, "version": 1, "uid": 20, "user": "bob", "changeset": 2, "tags": {"amenity": "bar"}}),
        ],
    )
    handler = ChangefileHandler(changefile_config, sequence_id=1)
    handler.apply_file(str(osc))
    assert set(handler.stats.keys()) == {1}
    assert set(u.username for u in handler.users.values()) == {"alice"}


def test_changefile_handler_valid_changesets_filter(osc_factory, changefile_config):
    """--hashtags pre-filter populates valid_changesets; only those changesets are kept."""
    osc = osc_factory(
        "007.osc",
        [
            ("node", {"id": 1, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {"amenity": "cafe"}}),
            ("node", {"id": 2, "version": 1, "uid": 20, "user": "bob", "changeset": 2, "tags": {"amenity": "bar"}}),
        ],
    )
    handler = ChangefileHandler(changefile_config, sequence_id=1, valid_changesets={1})
    handler.apply_file(str(osc))
    assert set(handler.stats.keys()) == {1}


def test_changefile_handler_hashtags_and_users_intersect(osc_factory, changefile_config):
    """--hashtags + --users → BOTH conditions must hold (regression: the user filter used to be silently ignored)."""
    changefile_config["whitelisted_users"] = ["alice"]
    osc = osc_factory(
        "intersect.osc",
        [
            # changeset 1 by alice, kept (in valid_changesets AND in whitelist)
            ("node", {"id": 1, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {"amenity": "cafe"}}),
            # changeset 1 by bob, dropped (in valid_changesets but NOT in whitelist)
            ("node", {"id": 2, "version": 1, "uid": 20, "user": "bob", "changeset": 1, "tags": {"amenity": "bar"}}),
            # changeset 3 by alice, dropped (in whitelist but NOT in valid_changesets)
            ("node", {"id": 3, "version": 1, "uid": 10, "user": "alice", "changeset": 3, "tags": {"amenity": "shop"}}),
        ],
    )
    handler = ChangefileHandler(changefile_config, sequence_id=1, valid_changesets={1, 2})
    handler.apply_file(str(osc))
    assert set(handler.stats.keys()) == {1}
    # Only alice's row was recorded under changeset 1.
    assert handler.stats[1].uid == 10
    assert {u.username for u in handler.users.values()} == {"alice"}


def test_changefile_handler_empty_valid_changesets_drops_everything(osc_factory, changefile_config):
    """Empty set means 'filter matched nothing', NOT 'no filter' (regression N2)."""
    osc = osc_factory(
        "emptyfilter.osc",
        [
            ("node", {"id": 1, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {"amenity": "cafe"}}),
            ("node", {"id": 2, "version": 1, "uid": 20, "user": "bob", "changeset": 2, "tags": {"amenity": "bar"}}),
        ],
    )
    handler = ChangefileHandler(changefile_config, sequence_id=1, valid_changesets=set())
    handler.apply_file(str(osc))
    assert handler.stats == {}
    assert handler.users == {}


def test_changefile_handler_none_valid_changesets_means_no_filter(osc_factory, changefile_config):
    """None means 'no filter active', collect everything that passes other filters."""
    osc = osc_factory(
        "nofilter.osc",
        [
            ("node", {"id": 1, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {"amenity": "cafe"}}),
            ("node", {"id": 2, "version": 1, "uid": 20, "user": "bob", "changeset": 2, "tags": {"amenity": "bar"}}),
        ],
    )
    handler = ChangefileHandler(changefile_config, sequence_id=1, valid_changesets=None)
    handler.apply_file(str(osc))
    assert set(handler.stats.keys()) == {1, 2}


def test_changeset_handler_geom_filter_intersects_not_centroid(tmp_path, changeset_config):
    """Border-straddling changesets must be kept (intersects), not silently dropped."""
    from shapely.geometry import box

    boundary = box(0, 0, 10, 10)
    changeset_config["geom_filter_wkt"] = boundary.wkt
    p = _write_changeset_xml(
        tmp_path,
        "cs_geom.osm",
        [
            {"id": 1, "bbox": (1, 1, 2, 2)},
            {"id": 2, "bbox": (9, 9, 12, 12)},
            {"id": 3, "bbox": (20, 20, 25, 25)},
            {"id": 4},
        ],
    )
    h = ChangesetHandler(changeset_config)
    h.apply_file(str(p))
    assert set(h.changesets.keys()) == {1, 2}


@pytest.mark.parametrize("version,expected_bucket", [(1, "c"), (2, "m"), (0, "d")])
def test_changefile_handler_action_dispatch(osc_factory, changefile_config, version, expected_bucket):
    """version=1 → CREATE, >1 → MODIFY, 0 → DELETE."""
    osc = osc_factory(
        f"action_{version}.osc",
        [
            (
                "node",
                {"id": 1, "version": version, "uid": 10, "user": "alice", "changeset": 1, "tags": {"amenity": "cafe"}},
            ),
        ],
    )
    handler = ChangefileHandler(changefile_config, sequence_id=1)
    handler.apply_file(str(osc))
    bucket = handler.stats[1].nodes
    assert getattr(bucket, expected_bucket) == 1
