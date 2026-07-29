"""Way-length rules in the live handler: open ways get haversine length, closed ways do not, and it is
always on (no config flag). Uses a tiny changefile so node locations resolve within the same diff."""

import datetime as dt
import gzip

from osmsg.handlers import ChangefileHandler

_OSC = """<?xml version="1.0" encoding="UTF-8"?>
<osmChange version="0.6"><create>
 <node id="1" version="1" timestamp="2026-07-01T00:00:00Z" uid="9" user="t" changeset="7" lat="0.0" lon="0.0"/>
 <node id="2" version="1" timestamp="2026-07-01T00:00:00Z" uid="9" user="t" changeset="7" lat="0.0" lon="1.0"/>
 <node id="10" version="1" timestamp="2026-07-01T00:00:00Z" uid="9" user="t" changeset="7" lat="5.0" lon="5.0"/>
 <node id="11" version="1" timestamp="2026-07-01T00:00:00Z" uid="9" user="t" changeset="7" lat="5.0" lon="5.001"/>
 <node id="12" version="1" timestamp="2026-07-01T00:00:00Z" uid="9" user="t" changeset="7" lat="5.001" lon="5.0"/>
 <way id="100" version="1" timestamp="2026-07-01T00:00:00Z" uid="9" user="t" changeset="7">
  <nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way>
 <way id="200" version="1" timestamp="2026-07-01T00:00:00Z" uid="9" user="t" changeset="7">
  <nd ref="10"/><nd ref="11"/><nd ref="12"/><nd ref="10"/><tag k="building" v="yes"/></way>
</create></osmChange>"""


def _run(tmp_path):
    path = tmp_path / "diff.osc.gz"
    with gzip.open(path, "wt") as fh:
        fh.write(_OSC)
    # No "length" key in the config: length is always on now, not gated by a flag.
    cfg = {
        "start_date_utc": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        "whitelisted_users": None,
        "tag_mode": "all",
        "additional_tags": None,
    }
    handler = ChangefileHandler(cfg, sequence_id=1)
    handler.apply_file(str(path), locations=True)
    tags = {row["k"]: row for stats in handler.stats.values() for row in stats.tags_list()}
    return tags


def test_open_way_gets_length(tmp_path):
    tags = _run(tmp_path)
    # ~1 degree of longitude at the equator is ~111 km.
    assert 110_000 < tags["highway"]["l"] < 112_000


def test_closed_way_gets_no_length(tmp_path):
    tags = _run(tmp_path)
    assert tags["building"]["l"] is None
