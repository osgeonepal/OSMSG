"""Shared layout helpers for the history datasets: Morton(centroid) sort plus partition writer."""

import pathlib

import duckdb

ROW_GROUP_SIZE = 100_000

MORTON_MACROS = """
CREATE OR REPLACE MACRO _s1(v) AS ((v | (v << 8)) & 16711935);
CREATE OR REPLACE MACRO _s2(v) AS ((_s1(v) | (_s1(v) << 4)) & 252645135);
CREATE OR REPLACE MACRO _s3(v) AS ((_s2(v) | (_s2(v) << 2)) & 858993459);
CREATE OR REPLACE MACRO _spread(v) AS ((_s3(v) | (_s3(v) << 1)) & 1431655765);
CREATE OR REPLACE MACRO morton2(lon, lat) AS (
    _spread(CAST(LEAST(65535, GREATEST(0, (COALESCE(lon, 0) + 180) / 360 * 65535)) AS BIGINT))
    | (_spread(CAST(LEAST(65535, GREATEST(0, (COALESCE(lat, 0) + 90) / 180 * 65535)) AS BIGINT)) << 1)
);
"""

GEOM_COLS = (
    "ST_X(ST_Centroid(c.geom)) AS lon, ST_Y(ST_Centroid(c.geom)) AS lat, "
    "ST_XMin(c.geom) AS min_lon, ST_YMin(c.geom) AS min_lat, "
    "ST_XMax(c.geom) AS max_lon, ST_YMax(c.geom) AS max_lat"
)


def write_partitions(
    con: duckdb.DuckDBPyConnection, view: str, base: pathlib.Path, order_by: str = "morton2(lon, lat)"
) -> None:
    """Write one parquet file per year/month partition, each sorted by `order_by`. `view` must expose
    integer `y`, `m` partition columns."""
    base.mkdir(parents=True, exist_ok=True)
    for year, month in con.execute(f"SELECT DISTINCT y, m FROM {view} ORDER BY y, m").fetchall():
        out = base / f"year={year}" / f"month={month}"
        out.mkdir(parents=True, exist_ok=True)
        con.execute(
            f"""COPY (SELECT * EXCLUDE (y, m) FROM {view} WHERE y={year} AND m={month} ORDER BY {order_by})
                TO '{out / "data.parquet"}' (FORMAT parquet, ROW_GROUP_SIZE {ROW_GROUP_SIZE})"""
        )
