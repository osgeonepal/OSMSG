"""Stats correctness, the central guarantees osmsg has to hold:

1. Hand-counted fixtures match the queried output exactly.
2. Processing the same .osc file twice does not double-count or drop anything.
3. Multi-worker (parallel) processing produces identical totals to single-worker.
4. Long-running changesets that span multiple replication files aggregate correctly.
5. No user is ever silently dropped, every uid in the input shows up in user_stats.
"""

from __future__ import annotations

import duckdb
import pytest
from shapely.geometry import box

from osmsg.db.ingest import flush_rows_to_parquet, merge_parquet_files
from osmsg.db.queries import attach_metadata, list_changesets, user_stats
from osmsg.db.schema import create_tables
from osmsg.handlers import ChangefileHandler, ChangesetHandler
from osmsg.pipeline import RunConfig, _resolve_valid_changesets


def _write_changeset_xml(tmp_path, name, changesets):
    """Pass `bbox=(min_lon, min_lat, max_lon, max_lat)` to emit min_*/max_* attributes
    that ChangesetHandler's geom filter can intersect-test against."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6">']
    for cs in changesets:
        attrs = (
            f'id="{cs["id"]}" created_at="{cs.get("created_at", "2026-04-27T20:00:00Z")}" '
            f'closed_at="{cs.get("closed_at", "2026-04-27T21:00:00Z")}" open="false" '
            f'num_changes="{cs.get("num_changes", 1)}" user="{cs.get("user", "alice")}" '
            f'uid="{cs.get("uid", 10)}" comments_count="0"'
        )
        if "bbox" in cs:
            min_lon, min_lat, max_lon, max_lat = cs["bbox"]
            attrs += f' min_lon="{min_lon}" min_lat="{min_lat}" max_lon="{max_lon}" max_lat="{max_lat}"'
        parts.append(f"  <changeset {attrs}>")
        for k, v in cs.get("tags", {}).items():
            parts.append(f'    <tag k="{k}" v="{v}"/>')
        parts.append("  </changeset>")
    parts.append("</osm>")
    p = tmp_path / name
    p.write_text("\n".join(parts), encoding="utf-8")
    return p


def _flush_changesets(handler: ChangesetHandler, parquet_dir, pid: int = 1, batch: int = 1):
    flush_rows_to_parquet(
        parquet_dir=parquet_dir,
        pid=pid,
        batch_index=batch,
        users=[u.to_row() for u in handler.users.values()],
        changesets=[c.to_row() for c in handler.changesets.values()],
    )


def _flush(handler: ChangefileHandler, parquet_dir, pid: int = 1, batch: int = 1):
    flush_rows_to_parquet(
        parquet_dir=parquet_dir,
        pid=pid,
        batch_index=batch,
        users=[u.to_row() for u in handler.users.values()],
        changesets=[c.to_row() for c in handler.stubs.values()],
        changeset_stats=[s.to_row() for s in handler.stats.values()],
    )


# 1) Hand-counted exact match


def test_user_stats_match_hand_counted_changes(tmp_path, osc_factory, changefile_config):
    """Build a deterministic .osc, run the full pipeline, assert every counter."""
    changefile_config["tag_mode"] = "all"
    changefile_config["additional_tags"] = None

    osc = osc_factory(
        "hand.osc",
        [
            # 5 node creates, 3 tagged → poi_created=3
            (
                "node",
                {"id": 1, "version": 1, "uid": 100, "user": "alice", "changeset": 1000, "tags": {"amenity": "cafe"}},
            ),
            (
                "node",
                {"id": 2, "version": 1, "uid": 100, "user": "alice", "changeset": 1000, "tags": {"shop": "bakery"}},
            ),
            (
                "node",
                {"id": 3, "version": 1, "uid": 100, "user": "alice", "changeset": 1000, "tags": {"amenity": "bench"}},
            ),
            ("node", {"id": 4, "version": 1, "uid": 100, "user": "alice", "changeset": 1000, "tags": {}}),
            ("node", {"id": 5, "version": 1, "uid": 100, "user": "alice", "changeset": 1000, "tags": {}}),
            # 2 node modifies, 1 tagged → poi_modified=1
            (
                "node",
                {"id": 6, "version": 2, "uid": 100, "user": "alice", "changeset": 1000, "tags": {"natural": "tree"}},
            ),
            ("node", {"id": 7, "version": 2, "uid": 100, "user": "alice", "changeset": 1000, "tags": {}}),
            # 1 node delete
            ("node", {"id": 8, "version": 0, "uid": 100, "user": "alice", "changeset": 1000, "tags": {}}),
            # 3 way creates
            (
                "way",
                {
                    "id": 10,
                    "version": 1,
                    "uid": 100,
                    "user": "alice",
                    "changeset": 1000,
                    "nodes": [1, 2, 3],
                    "tags": {"highway": "footway"},
                },
            ),
            (
                "way",
                {
                    "id": 11,
                    "version": 1,
                    "uid": 100,
                    "user": "alice",
                    "changeset": 1000,
                    "nodes": [4, 5, 6],
                    "tags": {"building": "yes"},
                },
            ),
            (
                "way",
                {
                    "id": 12,
                    "version": 1,
                    "uid": 100,
                    "user": "alice",
                    "changeset": 1000,
                    "nodes": [7, 8, 9],
                    "tags": {},
                },
            ),
            # 1 way modify
            (
                "way",
                {"id": 13, "version": 2, "uid": 100, "user": "alice", "changeset": 1000, "nodes": [1, 2], "tags": {}},
            ),
            # 1 relation create
            (
                "relation",
                {"id": 20, "version": 1, "uid": 100, "user": "alice", "changeset": 1000, "members": [], "tags": {}},
            ),
        ],
    )

    handler = ChangefileHandler(changefile_config, sequence_id=999)
    handler.apply_file(str(osc))

    # In-memory invariants
    s = handler.stats[1000]
    assert (s.nodes.c, s.nodes.m, s.nodes.d) == (5, 2, 1)
    assert (s.ways.c, s.ways.m, s.ways.d) == (3, 1, 0)
    assert (s.rels.c, s.rels.m, s.rels.d) == (1, 0, 0)
    assert s.poi_created == 3
    assert s.poi_modified == 1

    # Query-layer invariants (full round-trip through Parquet → DuckDB → SQL)
    parquet_dir = tmp_path / "parq"
    _flush(handler, parquet_dir)
    db = duckdb.connect(str(tmp_path / "test.duckdb"))
    create_tables(db)
    merge_parquet_files(db, parquet_dir, cleanup=False)

    rows = user_stats(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "alice"
    assert r["changesets"] == 1
    assert r["nodes_create"] == 5
    assert r["nodes_modify"] == 2
    assert r["nodes_delete"] == 1
    assert r["ways_create"] == 3
    assert r["ways_modify"] == 1
    assert r["ways_delete"] == 0
    assert r["rels_create"] == 1
    assert r["poi_create"] == 3
    assert r["poi_modify"] == 1
    # map_changes is the sum of the nine element columns, never the POI ones.
    assert r["map_changes"] == 5 + 2 + 1 + 3 + 1 + 0 + 1 + 0 + 0


# 2) Idempotency: same file processed twice


def test_processing_same_file_twice_yields_identical_stats(tmp_path, osc_factory, changefile_config):
    osc = osc_factory(
        "idem.osc",
        [
            ("node", {"id": 1, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {"amenity": "cafe"}}),
            ("node", {"id": 2, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {}}),
            (
                "way",
                {
                    "id": 1,
                    "version": 1,
                    "uid": 10,
                    "user": "alice",
                    "changeset": 1,
                    "nodes": [1, 2],
                    "tags": {"highway": "track"},
                },
            ),
        ],
    )

    db = duckdb.connect(str(tmp_path / "test.duckdb"))
    create_tables(db)

    for run_id in range(2):
        handler = ChangefileHandler(changefile_config, sequence_id=42)  # SAME seq_id both times
        handler.apply_file(str(osc))
        _flush(handler, tmp_path / f"parq_{run_id}")
        merge_parquet_files(db, tmp_path / f"parq_{run_id}", cleanup=False)

    rows = user_stats(db)
    assert len(rows) == 1
    alice = rows[0]
    assert alice["nodes_create"] == 2
    assert alice["ways_create"] == 1
    assert alice["map_changes"] == 3  # 2 + 1
    assert alice["changesets"] == 1

    # Defensive: the underlying changeset_stats table must contain exactly one row.
    cs_count = db.execute("SELECT COUNT(*) FROM changeset_stats").fetchone()[0]
    assert cs_count == 1, "INSERT OR IGNORE failed, duplicate stats rows from second pass"


# 3) Parallel equivalence: multiple workers vs single worker


def test_multi_worker_pipeline_matches_single_worker(tmp_path, osc_factory, changefile_config):
    """Same input across 4 files; processing them with 4 different pids must equal serial output."""
    files = []
    for f_idx in range(4):
        items = []
        for i in range(3):
            items.append(
                (
                    "node",
                    {
                        "id": (f_idx * 100) + i + 1,
                        "version": 1,
                        "uid": 10 + (i % 2),  # 2 distinct uids
                        "user": f"user{i % 2}",
                        "changeset": (f_idx * 10) + i + 1,
                        "tags": {"amenity": "cafe"} if i == 0 else {},
                    },
                )
            )
        files.append(osc_factory(f"f{f_idx}.osc", items))

    def run(parquet_dir, pid_per_file: bool):
        db = duckdb.connect(":memory:")
        create_tables(db)
        for i, f in enumerate(files):
            handler = ChangefileHandler(changefile_config, sequence_id=i + 1)
            handler.apply_file(str(f))
            _flush(handler, parquet_dir, pid=(10 + i) if pid_per_file else 1, batch=i + 1)
        merge_parquet_files(db, parquet_dir, cleanup=False)
        return sorted(user_stats(db), key=lambda r: r["uid"])

    serial = run(tmp_path / "parq_serial", pid_per_file=False)
    parallel = run(tmp_path / "parq_parallel", pid_per_file=True)

    assert len(serial) == len(parallel) == 2
    for sr, pr in zip(serial, parallel, strict=True):
        for k in ("uid", "name", "changesets", "nodes_create", "ways_create", "map_changes", "poi_create"):
            assert sr[k] == pr[k], f"mismatch on {k}: serial={sr[k]} parallel={pr[k]}"


# 4) Multi-file aggregation per user


def test_changeset_spread_across_files_aggregates(tmp_path, osc_factory, changefile_config):
    """Two replication files, same user, two distinct changesets → counts must sum."""
    f1 = osc_factory(
        "f1.osc",
        [
            ("node", {"id": 1, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {"amenity": "cafe"}}),
        ],
    )
    f2 = osc_factory(
        "f2.osc",
        [
            ("node", {"id": 2, "version": 1, "uid": 10, "user": "alice", "changeset": 2, "tags": {}}),
            ("way", {"id": 10, "version": 1, "uid": 10, "user": "alice", "changeset": 2, "nodes": [1, 2], "tags": {}}),
        ],
    )

    db = duckdb.connect(str(tmp_path / "agg.duckdb"))
    create_tables(db)
    for i, (f, seq) in enumerate([(f1, 100), (f2, 200)]):
        handler = ChangefileHandler(changefile_config, sequence_id=seq)
        handler.apply_file(str(f))
        _flush(handler, tmp_path / f"parq_{i}", pid=i)
        merge_parquet_files(db, tmp_path / f"parq_{i}", cleanup=False)

    rows = user_stats(db)
    assert len(rows) == 1
    alice = rows[0]
    assert alice["changesets"] == 2  # COUNT(DISTINCT changeset_id)
    assert alice["nodes_create"] == 2  # 1 from each file
    assert alice["ways_create"] == 1  # only in f2
    assert alice["poi_create"] == 1  # only the tagged node
    assert alice["map_changes"] == 3


# 5) No user is silently dropped


def test_every_uid_in_input_appears_in_user_stats(tmp_path, osc_factory, changefile_config):
    items = []
    for i in range(10):
        items.append(
            (
                "node",
                {
                    "id": i + 1,
                    "version": 1,
                    "uid": 100 + i,
                    "user": f"user{i}",
                    "changeset": i + 1,
                    "tags": {"amenity": "cafe"},
                },
            )
        )
    osc = osc_factory("ten.osc", items)

    handler = ChangefileHandler(changefile_config, sequence_id=999)
    handler.apply_file(str(osc))
    _flush(handler, tmp_path / "parq")

    db = duckdb.connect(str(tmp_path / "ten.duckdb"))
    create_tables(db)
    merge_parquet_files(db, tmp_path / "parq", cleanup=False)

    rows = user_stats(db)
    assert len(rows) == 10
    assert {r["name"] for r in rows} == {f"user{i}" for i in range(10)}
    assert {r["uid"] for r in rows} == {100 + i for i in range(10)}
    # Every user should have exactly 1 changeset, 1 poi_create
    assert all(r["changesets"] == 1 and r["poi_create"] == 1 for r in rows)


# 6) Different-pid worker shards are all picked up by merge


def test_merge_picks_up_every_worker_shard(tmp_path, osc_factory, changefile_config):
    """Simulates 8 worker processes writing to the same parquet dir; nothing must be missed."""
    osc = osc_factory(
        "shared.osc",
        [
            ("node", {"id": 1, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {}}),
        ],
    )

    parquet_dir = tmp_path / "shards"
    for pid in range(8):
        # Each worker processes the same file but emits a different (seq_id, changeset_id) row
        handler = ChangefileHandler(changefile_config, sequence_id=1000 + pid)
        handler.apply_file(str(osc))
        _flush(handler, parquet_dir, pid=pid, batch=1)

    db = duckdb.connect(":memory:")
    create_tables(db)
    merge_parquet_files(db, parquet_dir, cleanup=False)

    cs_count = db.execute("SELECT COUNT(*) FROM changeset_stats").fetchone()[0]
    assert cs_count == 8, f"expected 8 (seq_id, changeset_id) rows from 8 shards, got {cs_count}"

    rows = user_stats(db)
    assert len(rows) == 1
    assert rows[0]["changesets"] == 1  # COUNT(DISTINCT changeset_id), not COUNT(*)
    # nodes_create sums across all 8 shards
    assert rows[0]["nodes_create"] == 8


# 7) Hashtag pipeline ground-truth, the user-facing guarantee that motivates this tool.
#    A change to either ChangesetHandler or ChangefileHandler that drops a single
#    matched changeset, miscounts an element, or loses a hashtag must fail this test.
#    The fixture mirrors real-world quirks observed in OSM changeset replication:
#       - hashtag in `comment` only (the classic case)
#       - hashtag ONLY in the `hashtags` field (was the live regression: "Yovani V")
#       - hashtag in BOTH fields (must dedup, must not double-count edits)
#       - same hashtag in different case (substring filter must be case-insensitive)
#       - changeset that DOES NOT match the filter (must NOT appear and its edits
#         must NOT be counted, even though they sit in the same .osc file)


def test_hashtag_pipeline_end_to_end_no_contribution_lost(tmp_path, osc_factory, changefile_config):
    cs_xml = _write_changeset_xml(
        tmp_path,
        "cs.osm",
        [
            {"id": 1, "user": "alice", "uid": 10, "tags": {"comment": "Mapping #hotosm-project-1 buildings"}},
            {
                "id": 2,
                "user": "bob",
                "uid": 20,
                "tags": {"comment": "buildings", "hashtags": "#hotosm-project-2;#GEOSM"},
            },
            {
                "id": 3,
                "user": "carol",
                "uid": 30,
                "tags": {"comment": "Cleanup #HOTOSM-project-3", "hashtags": "#hotosm-project-3"},
            },
            {"id": 4, "user": "dave", "uid": 40, "tags": {"comment": "drobna poprawa", "hashtags": "#OzonGeo"}},
        ],
    )

    osc = osc_factory(
        "edits.osc",
        [
            (
                "node",
                {"id": 100, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {"amenity": "cafe"}},
            ),
            ("node", {"id": 101, "version": 1, "uid": 10, "user": "alice", "changeset": 1, "tags": {}}),
            (
                "way",
                {
                    "id": 200,
                    "version": 1,
                    "uid": 10,
                    "user": "alice",
                    "changeset": 1,
                    "nodes": [100, 101],
                    "tags": {"highway": "footway"},
                },
            ),
            ("node", {"id": 110, "version": 1, "uid": 20, "user": "bob", "changeset": 2, "tags": {"building": "yes"}}),
            ("node", {"id": 111, "version": 2, "uid": 20, "user": "bob", "changeset": 2, "tags": {"building": "yes"}}),
            ("way", {"id": 210, "version": 0, "uid": 20, "user": "bob", "changeset": 2, "nodes": [], "tags": {}}),
            (
                "relation",
                {
                    "id": 300,
                    "version": 1,
                    "uid": 30,
                    "user": "carol",
                    "changeset": 3,
                    "members": [],
                    "tags": {"type": "boundary"},
                },
            ),
            (
                "node",
                {"id": 120, "version": 1, "uid": 40, "user": "dave", "changeset": 4, "tags": {"amenity": "bench"}},
            ),
        ],
    )

    cs_cfg = {
        "hashtags": ["#hotosm"],
        "exact_lookup": False,
        "changeset_meta": False,
        "whitelisted_users": [],
        "geom_filter_wkt": None,
    }
    cs_handler = ChangesetHandler(cs_cfg)
    cs_handler.apply_file(str(cs_xml))

    cs_parquet = tmp_path / "cs_parq"
    _flush_changesets(cs_handler, cs_parquet)
    db = duckdb.connect(str(tmp_path / "e2e.duckdb"))
    create_tables(db)
    merge_parquet_files(db, cs_parquet, cleanup=False)

    valid = set(list_changesets(db))
    assert valid == {1, 2, 3}, "dave's #OzonGeo changeset must NOT be in the matched set"

    cf_cfg = dict(changefile_config)
    cf_cfg["hashtags"] = ["#hotosm"]
    cf_handler = ChangefileHandler(cf_cfg, sequence_id=1, valid_changesets=valid)
    cf_handler.apply_file(str(osc))

    cf_parquet = tmp_path / "cf_parq"
    _flush(cf_handler, cf_parquet, pid=2)
    merge_parquet_files(db, cf_parquet, cleanup=False)

    rows = user_stats(db)
    by_user = {r["name"]: r for r in rows}

    assert set(by_user) == {"alice", "bob", "carol"}, (
        "every matched user must appear; no unmatched user (dave) may leak in"
    )

    assert by_user["alice"]["changesets"] == 1
    assert by_user["alice"]["nodes_create"] == 2
    assert by_user["alice"]["ways_create"] == 1
    assert by_user["alice"]["poi_create"] == 1
    assert by_user["alice"]["map_changes"] == 3

    assert by_user["bob"]["changesets"] == 1
    assert by_user["bob"]["nodes_create"] == 1
    assert by_user["bob"]["nodes_modify"] == 1
    assert by_user["bob"]["ways_delete"] == 1
    assert by_user["bob"]["poi_create"] == 1
    assert by_user["bob"]["poi_modify"] == 1
    assert by_user["bob"]["map_changes"] == 3

    assert by_user["carol"]["changesets"] == 1
    assert by_user["carol"]["rels_create"] == 1
    assert by_user["carol"]["map_changes"] == 1

    attach_metadata(db, rows)
    by_user = {r["name"]: r for r in rows}
    assert any(h.lower() == "#hotosm-project-1" for h in by_user["alice"]["hashtags"])
    bob_ht = {h.lower() for h in by_user["bob"]["hashtags"]}
    assert "#hotosm-project-2" in bob_ht
    assert "#geosm" in bob_ht, "the `hashtags` field's secondary tags must persist for reporting"
    carol_ht = {h.lower() for h in by_user["carol"]["hashtags"]}
    assert "#hotosm-project-3" in carol_ht
    assert len(carol_ht) == 1, "duplicate hashtag in comment + hashtags field must be deduped case-insensitively"


def test_hashtag_pipeline_drops_unmatched_changeset_elements(tmp_path, osc_factory, changefile_config):
    """Element edits whose changeset doesn't match the hashtag filter must NOT be counted.

    Direct regression for `_should_collect`: empty `valid_changesets` means the filter
    matched nothing, drop everything, NOT 'no filter, keep everything'."""
    osc = osc_factory(
        "off.osc",
        [
            (
                "node",
                {"id": 1, "version": 1, "uid": 10, "user": "alice", "changeset": 999, "tags": {"amenity": "cafe"}},
            ),
        ],
    )

    cf_cfg = dict(changefile_config)
    cf_cfg["hashtags"] = ["#hotosm"]
    handler = ChangefileHandler(cf_cfg, sequence_id=1, valid_changesets=set())
    handler.apply_file(str(osc))

    parquet = tmp_path / "parq"
    _flush(handler, parquet)
    db = duckdb.connect(":memory:")
    create_tables(db)
    merge_parquet_files(db, parquet, cleanup=False)

    assert user_stats(db) == [], "no user may appear when the filter matched zero changesets"


def test_hashtag_filter_keeps_changeset_with_no_in_window_edits(tmp_path, changefile_config):
    """A matched changeset that has zero edits in the time window still belongs in the
    `changesets` table (so attach_metadata reports it), but contributes 0 to user_stats."""
    cs_xml = _write_changeset_xml(
        tmp_path,
        "cs_only.osm",
        [{"id": 7, "user": "eve", "uid": 70, "tags": {"hashtags": "#hotosm-project-7"}}],
    )
    cs_cfg = {
        "hashtags": ["#hotosm"],
        "exact_lookup": False,
        "changeset_meta": False,
        "whitelisted_users": [],
        "geom_filter_wkt": None,
    }
    cs_h = ChangesetHandler(cs_cfg)
    cs_h.apply_file(str(cs_xml))

    db = duckdb.connect(str(tmp_path / "empty_edits.duckdb"))
    create_tables(db)
    cs_parquet = tmp_path / "cs_parq"
    _flush_changesets(cs_h, cs_parquet)
    merge_parquet_files(db, cs_parquet, cleanup=False)

    assert set(list_changesets(db)) == {7}
    # No changefile processed → no rows in changeset_stats → user_stats is empty,
    # but the changeset metadata persists for downstream reporting.
    assert user_stats(db) == []
    cs_count = db.execute("SELECT COUNT(*) FROM changesets").fetchone()[0]
    assert cs_count == 1


def test_country_filter_drops_non_country_edits_end_to_end(tmp_path, osc_factory, changefile_config):
    """Full data-flow test for the --country boundary wiring."""
    cs_xml = _write_changeset_xml(
        tmp_path,
        "cs_geo.osm",
        [
            {"id": 1, "user": "binod", "uid": 100, "bbox": (84.21, 27.60, 84.30, 27.65)},
            {"id": 2, "user": "sita", "uid": 200, "bbox": (85.30, 27.70, 85.35, 27.72)},
            {"id": 3, "user": "tanaka", "uid": 300, "bbox": (139.69, 35.68, 139.77, 35.71)},
            {"id": 4, "user": "olivia", "uid": 400, "bbox": (-0.13, 51.49, -0.12, 51.51)},
        ],
    )
    cs_h = ChangesetHandler(
        {
            "hashtags": None,
            "exact_lookup": False,
            "changeset_meta": True,
            "whitelisted_users": [],
            "geom_filter_wkt": box(80.0, 26.0, 89.0, 31.0).wkt,
        }
    )
    cs_h.apply_file(str(cs_xml))
    assert set(cs_h.changesets.keys()) == {1, 2}

    db = duckdb.connect(str(tmp_path / "country.duckdb"))
    create_tables(db)
    _flush_changesets(cs_h, tmp_path / "cs_parq")
    merge_parquet_files(db, tmp_path / "cs_parq", cleanup=False)

    valid = _resolve_valid_changesets(db, RunConfig(countries=["nepal"]))
    assert valid == {1, 2}

    osc = osc_factory(
        "global.osc",
        [
            (
                "node",
                {"id": 10, "version": 1, "uid": 100, "user": "binod", "changeset": 1, "tags": {"amenity": "cafe"}},
            ),
            ("node", {"id": 20, "version": 1, "uid": 200, "user": "sita", "changeset": 2, "tags": {"shop": "bakery"}}),
            (
                "node",
                {"id": 30, "version": 1, "uid": 300, "user": "tanaka", "changeset": 3, "tags": {"amenity": "cafe"}},
            ),
            (
                "node",
                {"id": 40, "version": 1, "uid": 400, "user": "olivia", "changeset": 4, "tags": {"amenity": "pub"}},
            ),
        ],
    )
    cf_h = ChangefileHandler(changefile_config, sequence_id=1, valid_changesets=valid)
    cf_h.apply_file(str(osc))
    assert set(cf_h.stubs.keys()) == {1, 2}
    assert set(cf_h.users.keys()) == {100, 200}

    _flush(cf_h, tmp_path / "cf_parq", pid=2)
    merge_parquet_files(db, tmp_path / "cf_parq", cleanup=False)

    assert db.execute("SELECT COUNT(*) FROM changesets WHERE created_at IS NULL").fetchone()[0] == 0
    assert {r["name"] for r in user_stats(db)} == {"binod", "sita"}


@pytest.mark.parametrize(
    "cfg,expected_ids",
    [
        (RunConfig(), None),
        (RunConfig(hashtags=["#hotosm"]), {1, 2}),
        (RunConfig(boundary="/tmp/x.geojson"), {1, 2}),
        (RunConfig(countries=["nepal"]), {1, 2}),
        (RunConfig(countries=["nepal"], boundary="/tmp/x.geojson"), {1, 2}),
    ],
)
def test_resolve_valid_changesets_wiring(tmp_path, populated_db_factory, cfg, expected_ids):
    """No filter -> None (keep everything); any filter -> the seeded changeset_ids."""
    db = duckdb.connect(str(tmp_path / "wiring.duckdb"))
    create_tables(db)
    populated_db_factory(db)
    assert _resolve_valid_changesets(db, cfg) == expected_ids
