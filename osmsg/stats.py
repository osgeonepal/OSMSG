"""Single source of truth for how contribution stats are computed: schema and aggregation are fixed, so
the same numbers come out on any engine. Any change to what a stat means happens here alone."""

from __future__ import annotations

COUNT_COLS: tuple[str, ...] = (
    "nodes_created",
    "nodes_modified",
    "nodes_deleted",
    "ways_created",
    "ways_modified",
    "ways_deleted",
    "rels_created",
    "rels_modified",
    "rels_deleted",
    "poi_created",
    "poi_modified",
)
# map_changes counts real geometry edits: nodes, ways, relations. poi is a derived tag view, excluded.
MAP_CHANGES_COLS: tuple[str, ...] = tuple(c for c in COUNT_COLS if not c.startswith("poi"))

# A computed way length above this (metres) is treated as a geometry error, not a real length: no single
# OSM way spans 2000 km, so this catches null-island / corrupt-coordinate spans without dropping real ways.
MAX_WAY_LENGTH_M = 2_000_000.0

# The one per-changeset tag breakdown used everywhere (store, Postgres, published datasets): a DuckDB
# LIST<STRUCT> of (key, value, creates, modifies, length-in-metres `l`), or in Postgres a PG_TAG_TYPE array.
TAG_STRUCT_DDL = "STRUCT(k VARCHAR, v VARCHAR, c BIGINT, m BIGINT, l DOUBLE)"
PG_TAG_TYPE = "osmsg_tag"
PG_TAG_FIELDS = "k text, v text, c bigint, m bigint, l double precision"


def _col(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def map_changes_expr(prefix: str = "") -> str:
    """The one definition of map_changes: node + way + relation create/modify/delete."""
    return " + ".join(_col(prefix, c) for c in MAP_CHANGES_COLS)


def sum_cols(prefix: str = "", *, coalesce: bool = True) -> str:
    """`SUM(col) AS col` for every count column, canonical (schema) names."""
    parts = []
    for c in COUNT_COLS:
        expr = f"SUM({_col(prefix, c)})"
        parts.append(f"COALESCE({expr}, 0) AS {c}" if coalesce else f"{expr} AS {c}")
    return ", ".join(parts)


def map_changes_sum(prefix: str = "", *, alias: str = "map_changes") -> str:
    """`SUM(map_changes) AS map_changes`, coalesced."""
    return f"COALESCE(SUM({map_changes_expr(prefix)}), 0) AS {alias}"


def prefix_upper_bound(prefix: str) -> str:
    """Exclusive upper bound for a prefix range scan: the prefix with its last char incremented.
    So `col >= prefix AND col < prefix_upper_bound(prefix)` selects exactly the rows starting with
    `prefix`, and stays a range predicate the parquet reader can prune on (unlike ILIKE)."""
    if not prefix:
        raise ValueError("prefix must be non-empty")
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def tag_breakdown_from_list(scope: str, *, tags_col: str = "tags") -> str:
    """Tag key/value breakdown from a native LIST<STRUCT(k,v,c,m,l)> column (pre-exploded). `scope`
    is a relation already deduped to one row per changeset. Exact and fast: no per-query JSON parse."""
    return f"""
        SELECT t.k AS tag_key, t.v AS tag_value,
               SUM(t.c) AS creates, SUM(t.m) AS modifies, SUM(t.l) AS length_m
        FROM (SELECT UNNEST({tags_col}) AS t FROM {scope}) GROUP BY t.k, t.v
    """
