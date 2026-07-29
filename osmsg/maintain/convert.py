"""Convert a planet .osh history plus a changeset dump into the changefiles/changesets parquet
datasets, out of core via osmsg's own DuckDB tables.

Way length is reconstructed by a DuckDB join: the streaming pass emits each node's creation coords and
each open-way-create's node refs, then length is a parallel join (node coords + haversine) in the
aggregation stage. With no planet-sized node-location index, streaming has no cross-part dependency and
runs blob-split parallel across `parts`.
"""

import datetime as dt
import multiprocessing
import pathlib
import re
import shutil

import duckdb
import osmium
import pyarrow as pa
import pyarrow.parquet as pq

from ..db.schema import create_tables
from ..stats import MAX_WAY_LENGTH_M, TAG_STRUCT_DDL
from .parquet import GEOM_COLS, MORTON_MACROS, write_partitions
from .pbf_split import split_pbf

BATCH = 1_000_000
CREATE, MODIFY, DELETE = 0, 1, 2
DUCKDB_MEMORY_LIMIT = "48GB"
DUCKDB_THREADS = 32
TAG_SHARDS = 64

# Replicates osmium.geom.haversine_distance (R = 6372797.560856 m, per libosmium haversine.hpp) so join
# lengths equal osmium's; locked by test_convert_length_matches_osmium.
HAVERSINE_MACRO = """
CREATE OR REPLACE MACRO hav(lat1, lon1, lat2, lon2) AS
    2.0 * 6372797.560856 * asin(sqrt(
        pow(sin(radians(lat1 - lat2) * 0.5), 2)
        + cos(radians(lat1)) * cos(radians(lat2)) * pow(sin(radians(lon1 - lon2) * 0.5), 2)
    ));
"""

ELEM_SCHEMA = pa.schema(
    [
        ("changeset_id", pa.int64()),
        ("uid", pa.int64()),
        ("kind", pa.string()),
        ("action", pa.int8()),
        ("tagged", pa.int8()),
        ("ts", pa.timestamp("us", tz="UTC")),
    ]
)
TAG_SCHEMA = pa.schema(
    [
        ("changeset_id", pa.int64()),
        ("action", pa.int8()),
        ("tag_key", pa.string()),
        ("tag_value", pa.string()),
        # Join key to way_len for way tags; NULL for node/relation tags. Length is derived in the length
        # join and attached only to a way's create row.
        ("way_id", pa.int64()),
    ]
)
NODE_SCHEMA = pa.schema([("node_id", pa.int64()), ("lon", pa.float64()), ("lat", pa.float64())])
WAYNODE_SCHEMA = pa.schema([("way_id", pa.int64()), ("seq", pa.int32()), ("node_id", pa.int64())])
CS_SCHEMA = pa.schema(
    [
        ("changeset_id", pa.int64()),
        ("uid", pa.int64()),
        ("username", pa.string()),
        ("created_at", pa.timestamp("us", tz="UTC")),
        ("min_lon", pa.float64()),
        ("min_lat", pa.float64()),
        ("max_lon", pa.float64()),
        ("max_lat", pa.float64()),
        ("editor", pa.string()),
        ("hashtags", pa.list_(pa.string())),
    ]
)


class BatchWriter:
    """Buffers dict rows and flushes RecordBatches to one parquet file, bounding memory."""

    def __init__(self, path: pathlib.Path, schema: pa.Schema) -> None:
        self.schema = schema
        self.writer = pq.ParquetWriter(path, schema)
        self.buf: list[dict] = []

    def add(self, row: dict) -> None:
        self.buf.append(row)
        if len(self.buf) >= BATCH:
            self.flush()

    def flush(self) -> None:
        if self.buf:
            self.writer.write_table(pa.Table.from_pylist(self.buf, schema=self.schema))
            self.buf.clear()

    def close(self) -> None:
        self.flush()
        self.writer.close()


class ElementStreamer(osmium.SimpleHandler):
    """Streams elements without resolving locations: emits counts + tags in the window, plus every node's
    creation (v1) coords and every in-window open-way-create's node refs, for the length join downstream."""

    def __init__(
        self,
        start: dt.datetime,
        end: dt.datetime,
        elems: BatchWriter,
        tags: BatchWriter,
        nodes: BatchWriter,
        waynodes: BatchWriter,
    ) -> None:
        super().__init__()
        self.start, self.end = start, end
        self.elems, self.tags, self.nodes, self.waynodes = elems, tags, nodes, waynodes

    def _in_window(self, ts) -> bool:
        return self.start <= ts <= self.end

    def _emit(self, obj, kind: str, way_id: int | None = None) -> None:
        if not self._in_window(obj.timestamp):
            return
        action = DELETE if obj.deleted else (CREATE if obj.version == 1 else MODIFY)
        has_tags = bool(obj.tags)
        tagged = 1 if (kind == "node" and has_tags) else 0
        self.elems.add(
            {
                "changeset_id": obj.changeset,
                "uid": obj.uid,
                "kind": kind,
                "action": action,
                "tagged": tagged,
                "ts": obj.timestamp,
            }
        )
        if action == DELETE or not has_tags:
            return
        for k, v in obj.tags:
            self.tags.add(
                {"changeset_id": obj.changeset, "action": action, "tag_key": k, "tag_value": v, "way_id": way_id}
            )

    def node(self, n) -> None:
        self._emit(n, "node")
        # Creation coords for every node, window-independent: a way in the window can reference a node
        # created long before it. First version only, matching osmium's first-write-wins location index.
        if n.version == 1 and n.location.valid():
            self.nodes.add({"node_id": n.id, "lon": n.location.lon, "lat": n.location.lat})

    def way(self, w) -> None:
        self._emit(w, "way", way_id=w.id)
        # Node refs for an open way create in the window; length is joined per way_id downstream. Closed
        # ways (first ref == last ref) are areas and get no length, matching the live worker.
        if self._in_window(w.timestamp) and w.version == 1 and not w.deleted:
            nodes = w.nodes
            if len(nodes) >= 2 and nodes[0].ref != nodes[-1].ref:
                for seq, nd in enumerate(nodes):
                    self.waynodes.add({"way_id": w.id, "seq": seq, "node_id": nd.ref})

    def relation(self, r) -> None:
        self._emit(r, "relation")


class ChangesetStreamer(osmium.SimpleHandler):
    HASHTAG_RE = re.compile(r"#[\w-]+")

    def __init__(self, start: dt.datetime, end: dt.datetime, out: BatchWriter) -> None:
        super().__init__()
        self.start, self.end, self.out = start, end, out

    def changeset(self, c) -> None:
        created = c.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.UTC)
        if not (self.start <= created <= self.end):
            return
        bbox = (None, None, None, None)
        if c.bounds.valid():
            b = c.bounds
            bbox = (b.bottom_left.lon, b.bottom_left.lat, b.top_right.lon, b.top_right.lat)
        text = c.tags.get("comment", "") + "\n" + c.tags.get("hashtags", "")
        hashtags = list(dict.fromkeys(self.HASHTAG_RE.findall(text)))
        self.out.add(
            {
                "changeset_id": c.id,
                "uid": c.uid,
                "username": c.user,
                "created_at": created,
                "min_lon": bbox[0],
                "min_lat": bbox[1],
                "max_lon": bbox[2],
                "max_lat": bbox[3],
                "editor": c.tags.get("created_by"),
                "hashtags": hashtags,
            }
        )


def stream_elements(pbf: str, start: dt.datetime, end: dt.datetime, work: pathlib.Path, part: str) -> None:
    """Stream one PBF (or blob-split part) to its raw shards. No node-location index: each part is
    independent, so parts run in parallel and the length join stitches cross-part refs afterward."""
    work = pathlib.Path(work)
    elems = BatchWriter(work / f"raw_elements_{part}.parquet", ELEM_SCHEMA)
    tags = BatchWriter(work / f"raw_tags_{part}.parquet", TAG_SCHEMA)
    nodes = BatchWriter(work / f"raw_nodes_{part}.parquet", NODE_SCHEMA)
    waynodes = BatchWriter(work / f"raw_waynodes_{part}.parquet", WAYNODE_SCHEMA)
    ElementStreamer(start, end, elems, tags, nodes, waynodes).apply_file(pbf)
    for w in (elems, tags, nodes, waynodes):
        w.close()


def stream_changesets(dump: str, start: dt.datetime, end: dt.datetime, work: pathlib.Path) -> None:
    work = pathlib.Path(work)
    cs = BatchWriter(work / "raw_changesets.parquet", CS_SCHEMA)
    ChangesetStreamer(start, end, cs).apply_file(dump)
    cs.close()


def _build_way_len(con: duckdb.DuckDBPyConnection, work: pathlib.Path) -> None:
    """way_len(way_id, length): haversine over each open-way-create's node coords (node first-version).
    A way is dropped (no length) if any node lacks coords or the total exceeds MAX_WAY_LENGTH_M, matching
    osmium's InvalidLocationError / guard behaviour."""
    nodes = (work / "raw_nodes_*.parquet").as_posix()
    waynodes = (work / "raw_waynodes_*.parquet").as_posix()
    con.execute(HAVERSINE_MACRO)
    con.execute(
        f"""CREATE TABLE way_len AS
            WITH node_loc AS (
                SELECT node_id, any_value(lon) AS lon, any_value(lat) AS lat
                FROM read_parquet('{nodes}') GROUP BY node_id
            ),
            pts AS (
                SELECT wn.way_id, wn.seq, nl.lon, nl.lat
                FROM read_parquet('{waynodes}') wn LEFT JOIN node_loc nl USING (node_id)
            ),
            valid AS (
                SELECT way_id FROM pts GROUP BY way_id HAVING count(*) >= 2 AND count(*) = count(lon)
            ),
            seg AS (
                SELECT p.way_id,
                       hav(p.lat, p.lon, lag(p.lat) OVER w, lag(p.lon) OVER w) AS d
                FROM pts p SEMI JOIN valid v ON p.way_id = v.way_id
                WINDOW w AS (PARTITION BY p.way_id ORDER BY p.seq)
            )
            SELECT way_id, sum(d) AS length FROM seg GROUP BY way_id
            HAVING sum(d) <= {MAX_WAY_LENGTH_M}"""
    )


def build_tables(con: duckdb.DuckDBPyConnection, work: pathlib.Path) -> None:
    """Populate osmsg's tables (users, changesets, changeset_stats) from the streamed raw rows, deriving
    per-tag way length from the node-coord join."""
    work = pathlib.Path(work)
    cs = (work / "raw_changesets.parquet").as_posix()
    elems = (work / "raw_elements_*.parquet").as_posix()
    tags = (work / "raw_tags_*.parquet").as_posix()

    con.execute(f"INSERT INTO users SELECT uid, any_value(username) FROM read_parquet('{cs}') GROUP BY uid")
    con.execute(
        f"""INSERT INTO changesets
            SELECT changeset_id, uid, created_at, hashtags, editor,
                   CASE WHEN min_lon IS NOT NULL
                        THEN ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat) END
            FROM read_parquet('{cs}')"""
    )
    con.execute(
        f"""CREATE TABLE agg AS
            SELECT changeset_id, any_value(uid) uid,
                   count(*) FILTER (kind='node' AND action=0) nodes_created,
                   count(*) FILTER (kind='node' AND action=1) nodes_modified,
                   count(*) FILTER (kind='node' AND action=2) nodes_deleted,
                   count(*) FILTER (kind='way' AND action=0) ways_created,
                   count(*) FILTER (kind='way' AND action=1) ways_modified,
                   count(*) FILTER (kind='way' AND action=2) ways_deleted,
                   count(*) FILTER (kind='relation' AND action=0) rels_created,
                   count(*) FILTER (kind='relation' AND action=1) rels_modified,
                   count(*) FILTER (kind='relation' AND action=2) rels_deleted,
                   count(*) FILTER (kind='node' AND action=0 AND tagged=1) poi_created,
                   count(*) FILTER (kind='node' AND action=1 AND tagged=1) poi_modified,
                   min(ts) edited_at
            FROM read_parquet('{elems}') GROUP BY changeset_id"""
    )
    _build_way_len(con, work)
    # Attach length to the create row of each way tag only; node/relation tags and way modifies get none.
    shards = work / "tagshards"
    if shards.exists():
        shutil.rmtree(shards)
    con.execute(
        f"""COPY (
                SELECT t.changeset_id, t.action, t.tag_key, t.tag_value,
                       CASE WHEN t.action = 0 AND t.way_id IS NOT NULL THEN wl.length END AS len,
                       t.changeset_id % {TAG_SHARDS} AS shard
                FROM read_parquet('{tags}') t LEFT JOIN way_len wl ON t.way_id = wl.way_id
            )
            TO '{shards.as_posix()}' (FORMAT parquet, PARTITION_BY (shard))"""
    )
    cols = """a.nodes_created, a.nodes_modified, a.nodes_deleted,
              a.ways_created, a.ways_modified, a.ways_deleted,
              a.rels_created, a.rels_modified, a.rels_deleted,
              a.poi_created, a.poi_modified"""
    empty_tags = f"[]::{TAG_STRUCT_DDL}[]"
    for b in range(TAG_SHARDS):
        shard_dir = shards / f"shard={b}"
        if shard_dir.is_dir():
            shard_glob = (shard_dir / "*.parquet").as_posix()
            con.execute(
                f"""INSERT INTO changeset_stats
                    WITH t AS (
                        SELECT changeset_id, tag_key, tag_value,
                               count(*) FILTER (action=0) c, count(*) FILTER (action=1) m, sum(len) l
                        FROM read_parquet('{shard_glob}') GROUP BY changeset_id, tag_key, tag_value
                    ),
                    ts AS (
                        SELECT changeset_id, list(struct_pack(
                                   k := tag_key, v := tag_value, c := c, m := m, l := l)) AS tags
                        FROM t GROUP BY changeset_id
                    )
                    SELECT a.changeset_id, 0 AS seq_id, a.uid, {cols}, COALESCE(ts.tags, {empty_tags}) AS tags
                    FROM agg a LEFT JOIN ts USING (changeset_id)
                    WHERE a.changeset_id % {TAG_SHARDS} = {b}"""
            )
        else:
            con.execute(
                f"""INSERT INTO changeset_stats
                    SELECT a.changeset_id, 0 AS seq_id, a.uid, {cols}, {empty_tags} AS tags
                    FROM agg a WHERE a.changeset_id % {TAG_SHARDS} = {b}"""
            )
    shutil.rmtree(shards, ignore_errors=True)


def export_parquet(con: duckdb.DuckDBPyConnection, out: pathlib.Path) -> None:
    """Materialise the two datasets as persisted tables, then write Morton-sorted partitions."""
    con.execute(MORTON_MACROS)
    con.execute(
        f"""CREATE TABLE changefiles_all AS
            SELECT s.* EXCLUDE (seq_id),
                   COALESCE(c.created_at, a.edited_at) AS created_at, {GEOM_COLS},
                   year(COALESCE(c.created_at, a.edited_at)) y, month(COALESCE(c.created_at, a.edited_at)) m
            FROM changeset_stats s
            JOIN agg a USING (changeset_id)
            LEFT JOIN changesets c USING (changeset_id)"""
    )
    con.execute(
        f"""CREATE TABLE changesets_all AS
            SELECT c.changeset_id, c.uid, u.username, c.created_at, c.editor, c.hashtags, {GEOM_COLS},
                   year(c.created_at) y, month(c.created_at) m
            FROM changesets c LEFT JOIN users u USING (uid)"""
    )
    write_partitions(con, "changefiles_all", out / "changefiles")
    write_partitions(con, "changesets_all", out / "changesets")


def aggregate(work: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    """Build osmsg tables from the streamed raw rows and export the two parquet datasets to `out`."""
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "stats.duckdb"
    if db.exists():
        db.unlink()
    con = duckdb.connect(str(db))
    tmp = out / "duckdb_tmp"
    tmp.mkdir(exist_ok=True)
    con.execute(f"SET temp_directory='{tmp.as_posix()}'")
    con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET threads={DUCKDB_THREADS}")
    con.execute("SET preserve_insertion_order=false")
    create_tables(con)
    build_tables(con, work)
    export_parquet(con, out)
    con.close()
    return out


def convert(
    osh: str, changesets: str, start: dt.datetime, end: dt.datetime, work_dir: pathlib.Path, parts: int = 1
) -> pathlib.Path:
    """Convert one .osh history + changeset dump to the two parquet datasets under `work_dir/out`.
    `parts` blob-splits the history and streams the parts in parallel (no shared node index), then the
    length join runs over all shards; use it to scale streaming across cores."""
    work = pathlib.Path(work_dir)
    raw = work / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    if parts > 1:
        part_dir = work / "parts"
        part_dir.mkdir(parents=True, exist_ok=True)
        part_files = split_pbf(osh, part_dir, parts)
        with multiprocessing.Pool(len(part_files)) as pool:
            pool.starmap(
                stream_elements,
                [(str(pf), start, end, raw, f"{i:03d}") for i, pf in enumerate(part_files)],
            )
        shutil.rmtree(part_dir, ignore_errors=True)
    else:
        stream_elements(osh, start, end, raw, "000")
    stream_changesets(changesets, start, end, raw)
    return aggregate(raw, work / "out")
