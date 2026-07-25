"""maintain.rollup builds the user and hashtag_changeset rollups from a month's raw partitions,
and the derived all-time rollup is the exact sum of the month rollups."""

import duckdb

from osmsg.maintain.rollup import _COUNT_COLS, build_month_rollups

_TAGS_DDL = "STRUCT(k VARCHAR, v VARCHAR, c BIGINT, m BIGINT, len_m DOUBLE)[]"
_CF_COLS = ["changeset_id", "uid", *_COUNT_COLS, "tags"]


def _to_tags(nested):
    """Flatten a {key:{value:{c,m[,len]}}} test dict into the native LIST<STRUCT> the datasets now store."""
    if not nested:
        return []
    return [
        {"k": k, "v": v, "c": vv.get("c", 0), "m": vv.get("m", 0), "len_m": vv.get("len")}
        for k, by_value in nested.items()
        for v, vv in by_value.items()
    ]


def _write_month(out, year, month, changefiles, changesets):
    """changefiles rows: (changeset_id, uid, {count_col: n}, tag_dict|None).
    changesets rows: (changeset_id, uid, username, [hashtags]|None)."""
    con = duckdb.connect()
    cf_dir = out / "changefiles" / f"year={year}" / f"month={month}"
    cs_dir = out / "changesets" / f"year={year}" / f"month={month}"
    cf_dir.mkdir(parents=True)
    cs_dir.mkdir(parents=True)

    con.execute(
        f"CREATE TABLE cf ({', '.join(c + ' BIGINT' for c in _CF_COLS[:-1])}, tags {_TAGS_DDL}, created_at TIMESTAMP)"
    )
    con.executemany(
        f"INSERT INTO cf VALUES ({', '.join(['?'] * (len(_CF_COLS) + 1))})",
        [
            (
                cid,
                uid,
                *[counts.get(c, 0) for c in _COUNT_COLS],
                _to_tags(tags),
                f"{year}-{month:02d}-15",
            )
            for cid, uid, counts, tags in changefiles
        ],
    )
    con.execute(f"COPY cf TO '{cf_dir / 'data.parquet'}' (FORMAT parquet)")

    con.execute(
        "CREATE TABLE cs (changeset_id BIGINT, uid BIGINT, username VARCHAR, hashtags VARCHAR[], editor VARCHAR, "
        "lon DOUBLE, lat DOUBLE)"
    )
    con.executemany("INSERT INTO cs VALUES (?, ?, ?, ?, 'iD', NULL, NULL)", changesets)
    con.execute(f"COPY cs TO '{cs_dir / 'data.parquet'}' (FORMAT parquet)")
    con.close()


def _read(out, path):
    con = duckdb.connect()
    rows = con.execute(f"SELECT * FROM read_parquet('{out}/{path}')").fetchall()
    cols = [c[0] for c in con.description]
    con.close()
    return [dict(zip(cols, r, strict=True)) for r in rows]


def test_build_month_rollups(tmp_path):
    _write_month(
        tmp_path,
        2026,
        5,
        changefiles=[
            (1, 10, {"nodes_created": 5, "ways_created": 2, "poi_created": 1}, {"building": {"yes": {"c": 2, "m": 0}}}),
            (2, 10, {"nodes_created": 3}, {"highway": {"residential": {"c": 1, "m": 1}}}),
            (3, 20, {"nodes_modified": 4}, None),
        ],
        changesets=[
            (1, 10, "alice", ["#hotosm-project-1", "#test"]),
            (2, 10, "alice", ["#hotosm-project-2"]),
            (3, 20, "bob", None),
        ],
    )

    build_month_rollups(2026, 5, tmp_path)

    user = {r["uid"]: r for r in _read(tmp_path, "rollup/user/year=2026/month=5/data.parquet")}
    assert user[10]["changesets"] == 2
    assert user[10]["nodes_created"] == 8 and user[10]["ways_created"] == 2 and user[10]["poi_created"] == 1
    assert user[10]["map_changes"] == 10  # nodes+ways+rels, poi excluded
    assert user[20]["changesets"] == 1 and user[20]["map_changes"] == 4

    # One row per (hashtag, changeset), full payload; changeset 3 has no hashtag so it is absent.
    hc = {(r["hashtag"], r["changeset_id"]): r for r in _read(tmp_path, "rollup/hashtag_changeset/data.parquet")}
    assert set(hc) == {("#hotosm-project-1", 1), ("#test", 1), ("#hotosm-project-2", 2)}
    assert hc[("#hotosm-project-1", 1)]["nodes_created"] == 5 and hc[("#hotosm-project-1", 1)]["uid"] == 10
    assert hc[("#hotosm-project-2", 2)]["nodes_created"] == 3
    assert hc[("#hotosm-project-1", 1)]["tags"] == [{"k": "building", "v": "yes", "c": 2, "m": 0, "len_m": None}]

    users = {r["uid"]: r["username"] for r in _read(tmp_path, "rollup/users/data.parquet")}
    assert users == {10: "alice", 20: "bob"}

    alltime = {r["uid"]: r["map_changes"] for r in _read(tmp_path, "rollup/alltime_user/data.parquet")}
    assert alltime == {10: 10, 20: 4}


def test_alltime_is_sum_of_months(tmp_path):
    _write_month(tmp_path, 2026, 5, [(1, 10, {"nodes_created": 5}, None)], [(1, 10, "alice", None)])
    build_month_rollups(2026, 5, tmp_path)
    _write_month(tmp_path, 2026, 6, [(2, 10, {"nodes_created": 3}, None)], [(2, 10, "alice", None)])
    build_month_rollups(2026, 6, tmp_path)

    alltime = {r["uid"]: r for r in _read(tmp_path, "rollup/alltime_user/data.parquet")}
    assert alltime[10]["changesets"] == 2  # one changeset each month
    assert alltime[10]["nodes_created"] == 8
    assert alltime[10]["map_changes"] == 8
