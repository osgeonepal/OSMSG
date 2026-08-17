"""Adaptive chunking in merge_parquet_files: a large merge splits by changeset_id range, one chunk for a
small delta (unchanged), and the chunked result is identical to a single-chunk merge with the metadata
upsert (newest non-NULL wins) and dedup preserved."""

import datetime as dt

import duckdb

from osmsg.db import ingest
from osmsg.db.ingest import _id_ranges, flush_rows_to_parquet, merge_parquet_files
from osmsg.db.schema import create_tables
from osmsg.models import Changeset

UTC = dt.UTC


def _ids_parquet(path, ids):
    con = duckdb.connect()
    con.execute("CREATE TABLE t(changeset_id BIGINT)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in ids])
    con.execute(f"COPY t TO '{path}' (FORMAT parquet)")
    con.close()


def test_id_ranges_single_chunk_covers_all(tmp_path):
    p = tmp_path / "ids.parquet"
    _ids_parquet(p, [10, 20, 30])
    con = duckdb.connect()
    assert _id_ranges(con, str(p)) == [(10, 31)]  # one range, hi = max + 1
    con.close()


def test_id_ranges_splits_and_partitions(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_MERGE_CHUNK_ROWS", 2)
    p = tmp_path / "ids.parquet"
    ids = list(range(100, 110))  # 10 rows -> ceil(10/2) = 5 chunks
    _ids_parquet(p, ids)
    con = duckdb.connect()
    ranges = _id_ranges(con, str(p))
    con.close()
    assert len(ranges) == 5
    assert ranges[0][0] == 100 and ranges[-1][1] == 110  # covers [min, max+1)
    for (_, a_hi), (b_lo, _) in zip(ranges, ranges[1:], strict=False):
        assert a_hi == b_lo  # contiguous, no gap or overlap
    covered = {i for lo, hi in ranges for i in range(lo, hi)}
    assert set(ids) <= covered  # every id lands in exactly one range


def _shard(parquet_dir, changesets, batch):
    flush_rows_to_parquet(
        parquet_dir=parquet_dir,
        pid=1,
        batch_index=batch,
        users=[(c.uid, f"u{c.uid}") for c in changesets],
        changesets=[c.to_row() for c in changesets],
    )


def _merge_result(tmp_path, sub, chunk_rows, monkeypatch):
    monkeypatch.setattr(ingest, "_MERGE_CHUNK_ROWS", chunk_rows)
    ids = (100, 5_000, 90_000)
    full = [
        Changeset(
            changeset_id=cid,
            uid=1,
            created_at=dt.datetime(2026, 7, 1, tzinfo=UTC),
            hashtags=["#x"],
            editor="iD",
            bbox=(0, 0, 1, 1),
        )
        for cid in ids
    ]
    bare = [Changeset(changeset_id=cid, uid=1) for cid in ids]  # same ids, no metadata
    pdir = tmp_path / f"parq_{sub}"
    _shard(pdir, bare, batch=1)  # metadata-less emit first
    _shard(pdir, full, batch=2)  # newer non-NULL must win
    con = duckdb.connect(str(tmp_path / f"{sub}.duckdb"))
    create_tables(con)
    merge_parquet_files(con, pdir, cleanup=False)
    rows = con.execute(
        "SELECT changeset_id, editor, hashtags, created_at, geom IS NOT NULL FROM changesets ORDER BY changeset_id"
    ).fetchall()
    con.close()
    return rows


def test_chunked_merge_equals_single_and_upserts_metadata(tmp_path, monkeypatch):
    single = _merge_result(tmp_path, "single", 10**9, monkeypatch)  # one chunk
    chunked = _merge_result(tmp_path, "chunked", 1, monkeypatch)  # forced many chunks
    assert chunked == single  # chunking must not change the result
    assert [r[0] for r in chunked] == [100, 5_000, 90_000]  # one row per changeset (deduped)
    assert all(r[1] == "iD" and r[2] == ["#x"] and r[4] for r in chunked)  # metadata upserted, not lost
