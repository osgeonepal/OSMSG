"""The HF tag migration converts a JSON tag_stats changefiles partition to the native tags column."""

import duckdb

from osmsg.maintain.migrate import convert_partition, needs_migration
from osmsg.maintain.parquet import MORTON_MACROS


def test_convert_partition_json_to_native(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL json; LOAD json;")
    con.execute(MORTON_MACROS)
    src = tmp_path / "src.parquet"
    con.execute(
        f"""COPY (SELECT * FROM (VALUES
            (100, 5, '{{"building":{{"yes":{{"c":1,"m":0}}}}}}'::JSON, 13.1, 52.4),
            (200, 3, NULL::JSON, 13.2, 52.5),
            (300, 0, '{{"highway":{{"residential":{{"c":2,"m":1,"len":50.5}}}}}}'::JSON, 13.3, 52.6)
        ) AS t(changeset_id, nodes_created, tag_stats, lon, lat))
        TO '{src}' (FORMAT parquet)"""
    )
    assert needs_migration(con, str(src))

    dest = tmp_path / "out" / "data.parquet"
    convert_partition(con, str(src), dest)

    assert not needs_migration(con, str(dest))  # tag_stats gone, native tags present
    rows = {r[0]: r[1] for r in con.execute(f"SELECT changeset_id, tags FROM read_parquet('{dest}')").fetchall()}

    def tagset(tags):
        return {(t["k"], t["v"], t["c"], t["m"], t["len_m"]) for t in (tags or [])}

    assert tagset(rows[100]) == {("building", "yes", 1, 0, None)}
    assert tagset(rows[200]) == set()  # NULL tag_stats -> empty native list
    assert tagset(rows[300]) == {("highway", "residential", 2, 1, 50.5)}
