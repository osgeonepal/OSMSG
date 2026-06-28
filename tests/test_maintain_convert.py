"""Correctness of the planet converter on synthetic .osh + changeset inputs (offline, no planet)."""

import datetime as dt
import json
import pathlib

import duckdb
import osmium
import osmium.osm.mutable as mut

from osmsg.maintain.convert import convert

UTC = dt.UTC

CHANGESET_DUMP = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6">
 <changeset id="100" created_at="2022-03-01T00:00:00Z" uid="1" user="alice"
   min_lon="13.0" min_lat="52.3" max_lon="13.2" max_lat="52.5" num_changes="2" open="false">
  <tag k="created_by" v="JOSM"/>
  <tag k="comment" v="adding buildings #hotosm"/>
 </changeset>
 <changeset id="200" created_at="2023-06-01T00:00:00Z" uid="1" user="alice"
   min_lon="13.0" min_lat="52.3" max_lon="13.2" max_lat="52.5" num_changes="1" open="false">
  <tag k="created_by" v="iD"/>
 </changeset>
 <changeset id="300" created_at="2024-01-01T00:00:00Z" uid="2" user="bob"
   min_lon="13.0" min_lat="52.3" max_lon="13.2" max_lat="52.5" num_changes="1" open="false"/>
 <changeset id="50" created_at="2019-01-01T00:00:00Z" uid="1" user="alice"
   min_lon="0" min_lat="0" max_lon="1" max_lat="1" num_changes="1" open="false"/>
</osm>
"""


def _build_history(path: str) -> None:
    """One node created (cs100, 2022) / modified (cs200, 2023) / deleted (cs300, 2024), a tagged way in
    cs100, and a pre-window node (cs50, 2019) that the window must exclude."""
    writer = osmium.SimpleWriter(path)
    common = {"uid": 1, "user": "alice", "location": (10.0, 20.0)}
    writer.add_node(
        mut.Node(
            id=1,
            version=1,
            visible=True,
            timestamp="2022-03-01T00:00:00Z",
            changeset=100,
            tags={"building": "yes"},
            **common,
        )
    )
    writer.add_node(
        mut.Node(
            id=1,
            version=2,
            visible=True,
            timestamp="2023-06-01T00:00:00Z",
            changeset=200,
            tags={"building": "house"},
            **common,
        )
    )
    writer.add_node(
        mut.Node(
            id=1,
            version=3,
            visible=False,
            timestamp="2024-01-01T00:00:00Z",
            changeset=300,
            uid=2,
            user="bob",
            location=(10.0, 20.0),
        )
    )
    writer.add_way(
        mut.Way(
            id=10,
            version=1,
            visible=True,
            timestamp="2022-03-01T01:00:00Z",
            changeset=100,
            uid=1,
            user="alice",
            tags={"highway": "residential"},
            nodes=[1],
        )
    )
    writer.add_node(
        mut.Node(
            id=2,
            version=1,
            visible=True,
            timestamp="2019-01-01T00:00:00Z",
            changeset=50,
            uid=1,
            user="alice",
            tags={"building": "yes"},
            location=(11.0, 21.0),
        )
    )
    writer.close()


def test_convert_attribution_tags_window(tmp_path):
    osh = str(tmp_path / "hist.osh.pbf")
    dump = str(tmp_path / "changesets.osm")
    _build_history(osh)
    pathlib.Path(dump).write_text(CHANGESET_DUMP)

    out = convert(osh, dump, dt.datetime(2021, 1, 1, tzinfo=UTC), dt.datetime(2025, 1, 1, tzinfo=UTC), tmp_path)

    con = duckdb.connect()
    cf = {
        r[0]: r
        for r in con.execute(
            f"SELECT changeset_id, nodes_created, nodes_modified, nodes_deleted, ways_created, "
            f"poi_created, tag_stats, lon FROM read_parquet('{out}/changefiles/**/*.parquet', hive_partitioning=true)"
        ).fetchall()
    }
    assert set(cf) == {100, 200, 300}
    assert cf[100][1] == 1 and cf[100][4] == 1
    assert cf[200][2] == 1
    assert cf[300][3] == 1
    assert cf[100][5] == 1
    assert abs(cf[100][7] - 13.1) < 1e-6

    ts100 = json.loads(cf[100][6])
    assert ts100["building"]["yes"] == {"c": 1, "m": 0}
    assert ts100["highway"]["residential"] == {"c": 1, "m": 0}
    assert json.loads(cf[200][6])["building"]["house"] == {"c": 0, "m": 1}
    assert cf[300][6] is None

    changesets = {
        r[0]: r
        for r in con.execute(
            f"SELECT changeset_id, uid, editor, hashtags, lon FROM "
            f"read_parquet('{out}/changesets/**/*.parquet', hive_partitioning=true)"
        ).fetchall()
    }
    assert set(changesets) == {100, 200, 300}
    assert changesets[100][2] == "JOSM" and changesets[100][3] == ["#hotosm"]
    assert changesets[300][2] is None

    db = duckdb.connect(str(out / "stats.duckdb"), read_only=True)
    assert db.execute("SELECT count(*) FROM changeset_stats").fetchone()[0] == 3
    assert db.execute("SELECT count(*) FROM changesets").fetchone()[0] == 3
    assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 2
