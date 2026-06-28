"""Convert a planet .osh history plus a changeset dump into the changefiles/changesets parquet
datasets, out of core via osmsg's own DuckDB tables."""

import concurrent.futures as cf
import datetime as dt
import pathlib
import re
import shutil

import duckdb
import osmium
import pyarrow as pa
import pyarrow.parquet as pq

from ..db.schema import create_tables
from .parquet import GEOM_COLS, MORTON_MACROS, write_partitions
from .pbf_split import split_pbf

BATCH = 1_000_000
CREATE, MODIFY, DELETE = 0, 1, 2
DUCKDB_MEMORY_LIMIT = "40GB"
DUCKDB_THREADS = 24
TAG_SHARDS = 64

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
    [("changeset_id", pa.int64()), ("action", pa.int8()), ("tag_key", pa.string()), ("tag_value", pa.string())]
)
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
    def __init__(self, start: dt.datetime, end: dt.datetime, elems: BatchWriter, tags: BatchWriter) -> None:
        super().__init__()
        self.start, self.end, self.elems, self.tags = start, end, elems, tags

    def _emit(self, obj, kind: str) -> None:
        ts = obj.timestamp
        if not (self.start <= ts <= self.end):
            return
        action = DELETE if obj.deleted else (CREATE if obj.version == 1 else MODIFY)
        has_tags = bool(obj.tags)
        tagged = 1 if (kind == "node" and has_tags) else 0
        self.elems.add(
            {"changeset_id": obj.changeset, "uid": obj.uid, "kind": kind, "action": action, "tagged": tagged, "ts": ts}
        )
        if action == DELETE or not has_tags:
            return
        for k, v in obj.tags:
            self.tags.add({"changeset_id": obj.changeset, "action": action, "tag_key": k, "tag_value": v})

    def node(self, n) -> None:
        self._emit(n, "node")

    def way(self, w) -> None:
        self._emit(w, "way")

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
    """Stream one PBF (or one split part) to raw_elements_<part>.parquet + raw_tags_<part>.parquet."""
    work = pathlib.Path(work)
    elems = BatchWriter(work / f"raw_elements_{part}.parquet", ELEM_SCHEMA)
    tags = BatchWriter(work / f"raw_tags_{part}.parquet", TAG_SCHEMA)
    ElementStreamer(start, end, elems, tags).apply_file(pbf)
    elems.close()
    tags.close()


def stream_changesets(dump: str, start: dt.datetime, end: dt.datetime, work: pathlib.Path) -> None:
    work = pathlib.Path(work)
    cs = BatchWriter(work / "raw_changesets.parquet", CS_SCHEMA)
    ChangesetStreamer(start, end, cs).apply_file(dump)
    cs.close()


def build_tables(con: duckdb.DuckDBPyConnection, work: pathlib.Path) -> None:
    """Populate osmsg's tables (users, changesets, changeset_stats) from the streamed raw rows."""
    con.execute("INSTALL json; LOAD json;")
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
    shards = work / "tagshards"
    if shards.exists():
        shutil.rmtree(shards)
    con.execute(
        f"""COPY (SELECT changeset_id, action, tag_key, tag_value, changeset_id % {TAG_SHARDS} AS shard
                  FROM read_parquet('{tags}'))
            TO '{shards.as_posix()}' (FORMAT parquet, PARTITION_BY (shard))"""
    )
    cols = """a.nodes_created, a.nodes_modified, a.nodes_deleted,
              a.ways_created, a.ways_modified, a.ways_deleted,
              a.rels_created, a.rels_modified, a.rels_deleted,
              a.poi_created, a.poi_modified"""
    for b in range(TAG_SHARDS):
        shard_dir = shards / f"shard={b}"
        if shard_dir.is_dir():
            shard_glob = (shard_dir / "*.parquet").as_posix()
            con.execute(
                f"""INSERT INTO changeset_stats
                    WITH t AS (
                        SELECT changeset_id, tag_key, tag_value,
                               count(*) FILTER (action=0) c, count(*) FILTER (action=1) m
                        FROM read_parquet('{shard_glob}') GROUP BY changeset_id, tag_key, tag_value
                    ),
                    byval AS (
                        SELECT changeset_id, tag_key,
                               json_group_object(tag_value, json_object('c', c, 'm', m)) vals
                        FROM t GROUP BY changeset_id, tag_key
                    ),
                    ts AS (
                        SELECT changeset_id, json_group_object(tag_key, vals) AS tag_stats
                        FROM byval GROUP BY changeset_id
                    )
                    SELECT a.changeset_id, 0 AS seq_id, a.uid, {cols}, ts.tag_stats
                    FROM agg a LEFT JOIN ts USING (changeset_id)
                    WHERE a.changeset_id % {TAG_SHARDS} = {b}"""
            )
        else:
            con.execute(
                f"""INSERT INTO changeset_stats
                    SELECT a.changeset_id, 0 AS seq_id, a.uid, {cols}, NULL AS tag_stats
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
    """Convert one .osh history + changeset dump to the two parquet datasets under `work_dir/out`,
    returned as a path. With parts>1 the history is split and streamed concurrently."""
    work = pathlib.Path(work_dir)
    raw = work / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    if parts <= 1:
        stream_elements(osh, start, end, raw, "000")
        stream_changesets(changesets, start, end, raw)
    else:
        part_paths = split_pbf(osh, work / "parts", parts)
        with cf.ProcessPoolExecutor(max_workers=parts) as ex:
            futures = [
                ex.submit(stream_elements, p.as_posix(), start, end, raw, p.stem.removeprefix("part"))
                for p in part_paths
            ]
            futures.append(ex.submit(stream_changesets, changesets, start, end, raw))
            for fut in cf.as_completed(futures):
                fut.result()
    return aggregate(raw, work / "out")
