import json
from datetime import datetime
from typing import Any

from .db import get_pool

GEOMETRY_MODE_GEOM = "geom"
GEOMETRY_MODE_BBOX = "bbox"


def _map_changes_expr(alias: str = "st") -> str:
    return f"""
        {alias}.nodes_created + {alias}.nodes_modified + {alias}.nodes_deleted +
        {alias}.ways_created + {alias}.ways_modified + {alias}.ways_deleted +
        {alias}.rels_created + {alias}.rels_modified + {alias}.rels_deleted
    """


_TAG_VALUE_CTES = """,
        tag_agg AS (
            SELECT
                st.uid,
                tk.key                                  AS tag_key,
                tv.key                                  AS tag_val,
                SUM(COALESCE((tv.value->>'c')::bigint, 0))  AS total_c,
                SUM(COALESCE((tv.value->>'m')::bigint, 0))  AS total_m,
                SUM((tv.value->>'len')::double precision)   AS total_len
            FROM stats_scope st
            JOIN LATERAL jsonb_each(st.tag_stats) tk ON st.tag_stats IS NOT NULL
            JOIN LATERAL jsonb_each(tk.value)     tv ON true
            GROUP BY st.uid, tk.key, tv.key
        ),
        tag_per_key AS (
            SELECT
                uid,
                tag_key,
                jsonb_object_agg(
                    tag_val,
                    CASE WHEN total_len IS NOT NULL
                        THEN jsonb_build_object('c', total_c, 'm', total_m, 'len', total_len)
                        ELSE jsonb_build_object('c', total_c, 'm', total_m)
                    END
                ) AS tag_vals
            FROM tag_agg
            GROUP BY uid, tag_key
        ),
        tag_per_user AS (
            SELECT uid, jsonb_object_agg(tag_key, tag_vals) AS tag_stats
            FROM tag_per_key
            GROUP BY uid
        )"""

_TAG_KEY_CTES = """,
        tag_key_agg AS (
            SELECT
                st.uid,
                tk.key AS tag_key,
                SUM(COALESCE(key_stats.total_c, 0)) AS total_c,
                SUM(COALESCE(key_stats.total_m, 0)) AS total_m,
                SUM(key_stats.total_len) AS total_len
            FROM stats_scope st
            JOIN LATERAL jsonb_each(st.tag_stats) tk ON st.tag_stats IS NOT NULL
            JOIN LATERAL (
                SELECT
                    SUM(COALESCE((tv.value->>'c')::bigint, 0)) AS total_c,
                    SUM(COALESCE((tv.value->>'m')::bigint, 0)) AS total_m,
                    SUM((tv.value->>'len')::double precision) AS total_len
                FROM jsonb_each(tk.value) tv
            ) key_stats ON true
            GROUP BY st.uid, tk.key
        ),
        tag_per_user AS (
            SELECT
                uid,
                jsonb_object_agg(
                    tag_key,
                    jsonb_build_object('c', total_c, 'm', total_m, 'len', total_len)
                ) AS tag_stats
            FROM tag_key_agg
            GROUP BY uid
        )"""

_HASHTAG_CTE = """,
        user_hashtags AS (
            SELECT
                st.uid,
                ARRAY_AGG(DISTINCT ht.hashtag ORDER BY ht.hashtag) AS hashtags
            FROM stats_scope st
            JOIN changesets cs ON cs.changeset_id = st.changeset_id
            CROSS JOIN LATERAL UNNEST(cs.hashtags) AS ht(hashtag)
            WHERE cs.hashtags IS NOT NULL
            GROUP BY st.uid
        )"""


def _column(alias: str, name: str) -> str:
    return f"{alias}.{name}" if alias else name


def _bbox_centroid_geojson_sql(alias: str = "cs") -> str:
    min_lon = _column(alias, "min_lon")
    min_lat = _column(alias, "min_lat")
    max_lon = _column(alias, "max_lon")
    max_lat = _column(alias, "max_lat")
    return f"""
        CASE
            WHEN {min_lon} IS NULL OR {min_lat} IS NULL OR {max_lon} IS NULL OR {max_lat} IS NULL THEN NULL
            ELSE jsonb_build_object(
                'type', 'Point',
                'coordinates', jsonb_build_array(
                    ({min_lon} + {max_lon}) / 2.0,
                    ({min_lat} + {max_lat}) / 2.0
                )
            )::TEXT
        END
    """


def _user_stats_sql(
    *,
    filter_dates: bool,
    filter_hashtags: bool,
    tag_mode: str,
) -> str:
    n = 1
    include_tags = tag_mode != "none"
    changeset_filters: list[str] = []

    if filter_dates:
        changeset_filters.append(f"created_at >= ${n}")
        n += 1
        changeset_filters.append(f"created_at < ${n}")
        n += 1

    if filter_hashtags:
        changeset_filters.append(f"hashtags && ${n}::TEXT[]")
        n += 1

    limit_param = f"${n}"
    n += 1
    offset_param = f"${n}"

    # No filter -> all stats (orphans included); any filter -> JOIN through changesets.
    if changeset_filters:
        scope_cte = f"""
        WITH filtered_changesets AS (
            SELECT changeset_id FROM changesets WHERE {" AND ".join(changeset_filters)}
        ),
        stats_scope AS (
            SELECT st.*
            FROM changeset_stats st
            JOIN filtered_changesets fc ON st.changeset_id = fc.changeset_id
        )"""
    else:
        scope_cte = "WITH stats_scope AS (SELECT * FROM changeset_stats)"

    map_changes = """COALESCE(SUM(
                st.nodes_created + st.nodes_modified + st.nodes_deleted +
                st.ways_created + st.ways_modified + st.ways_deleted +
                st.rels_created + st.rels_modified + st.rels_deleted
            ), 0)"""

    # Rank and page users first so the expensive hashtag/tag expansion runs only for the
    # returned top-N users, not every user in the window.
    ranked_cte = f""",
        ranked AS (
            SELECT
                st.uid,
                COUNT(DISTINCT st.changeset_id) AS changesets,
                COALESCE(SUM(st.nodes_created), 0) AS nodes_create,
                COALESCE(SUM(st.nodes_modified), 0) AS nodes_modify,
                COALESCE(SUM(st.nodes_deleted), 0) AS nodes_delete,
                COALESCE(SUM(st.ways_created), 0) AS ways_create,
                COALESCE(SUM(st.ways_modified), 0) AS ways_modify,
                COALESCE(SUM(st.ways_deleted), 0) AS ways_delete,
                COALESCE(SUM(st.rels_created), 0) AS rels_create,
                COALESCE(SUM(st.rels_modified), 0) AS rels_modify,
                COALESCE(SUM(st.rels_deleted), 0) AS rels_delete,
                COALESCE(SUM(st.poi_created), 0) AS poi_create,
                COALESCE(SUM(st.poi_modified), 0) AS poi_modify,
                {map_changes} AS map_changes,
                ROW_NUMBER() OVER (ORDER BY {map_changes} DESC, st.uid ASC) AS rank
            FROM stats_scope st
            GROUP BY st.uid
            ORDER BY map_changes DESC, st.uid ASC
            LIMIT {limit_param} OFFSET {offset_param}
        )"""

    hashtag_cte = """,
        user_hashtags AS (
            SELECT
                st.uid,
                ARRAY_AGG(DISTINCT ht.hashtag ORDER BY ht.hashtag) AS hashtags
            FROM stats_scope st
            JOIN ranked r ON r.uid = st.uid
            JOIN changesets cs ON cs.changeset_id = st.changeset_id
            CROSS JOIN LATERAL UNNEST(cs.hashtags) AS ht(hashtag)
            WHERE cs.hashtags IS NOT NULL
            GROUP BY st.uid
        )"""

    if tag_mode == "keys":
        tag_ctes = """,
        tag_key_agg AS (
            SELECT
                st.uid,
                tk.key AS tag_key,
                SUM(COALESCE(key_stats.total_c, 0)) AS total_c,
                SUM(COALESCE(key_stats.total_m, 0)) AS total_m,
                SUM(key_stats.total_len) AS total_len
            FROM stats_scope st
            JOIN ranked r ON r.uid = st.uid
            JOIN LATERAL jsonb_each(st.tag_stats) tk ON st.tag_stats IS NOT NULL
            JOIN LATERAL (
                SELECT
                    SUM(COALESCE((tv.value->>'c')::bigint, 0)) AS total_c,
                    SUM(COALESCE((tv.value->>'m')::bigint, 0)) AS total_m,
                    SUM((tv.value->>'len')::double precision) AS total_len
                FROM jsonb_each(tk.value) tv
            ) key_stats ON true
            GROUP BY st.uid, tk.key
        ),
        tag_per_user AS (
            SELECT
                uid,
                jsonb_object_agg(
                    tag_key,
                    jsonb_build_object('c', total_c, 'm', total_m, 'len', total_len)
                ) AS tag_stats
            FROM tag_key_agg
            GROUP BY uid
        )"""
    elif tag_mode == "all":
        tag_ctes = """,
        tag_agg AS (
            SELECT
                st.uid,
                tk.key                                     AS tag_key,
                tv.key                                     AS tag_val,
                SUM(COALESCE((tv.value->>'c')::bigint, 0)) AS total_c,
                SUM(COALESCE((tv.value->>'m')::bigint, 0)) AS total_m,
                SUM((tv.value->>'len')::double precision)  AS total_len
            FROM stats_scope st
            JOIN ranked r ON r.uid = st.uid
            JOIN LATERAL jsonb_each(st.tag_stats) tk ON st.tag_stats IS NOT NULL
            JOIN LATERAL jsonb_each(tk.value)     tv ON true
            GROUP BY st.uid, tk.key, tv.key
        ),
        tag_per_key AS (
            SELECT
                uid,
                tag_key,
                jsonb_object_agg(
                    tag_val,
                    CASE WHEN total_len IS NOT NULL
                        THEN jsonb_build_object('c', total_c, 'm', total_m, 'len', total_len)
                        ELSE jsonb_build_object('c', total_c, 'm', total_m)
                    END
                ) AS tag_vals
            FROM tag_agg
            GROUP BY uid, tag_key
        ),
        tag_per_user AS (
            SELECT uid, jsonb_object_agg(tag_key, tag_vals) AS tag_stats
            FROM tag_per_key
            GROUP BY uid
        )"""
    else:
        tag_ctes = ""

    tag_select = "COALESCE(tpu.tag_stats, '{}'::jsonb) AS tag_stats" if include_tags else "NULL::jsonb AS tag_stats"
    tag_join = "LEFT JOIN tag_per_user tpu ON tpu.uid = r.uid" if include_tags else ""

    return f"""
        {scope_cte}{ranked_cte}{hashtag_cte}{tag_ctes}
        SELECT
            r.uid,
            u.username AS name,
            r.changesets,
            r.nodes_create, r.nodes_modify, r.nodes_delete,
            r.ways_create, r.ways_modify, r.ways_delete,
            r.rels_create, r.rels_modify, r.rels_delete,
            r.poi_create, r.poi_modify,
            r.map_changes,
            r.rank,
            COALESCE(uh.hashtags, ARRAY[]::TEXT[]) AS hashtags,
            {tag_select}
        FROM ranked r
        JOIN users u ON u.uid = r.uid
        LEFT JOIN user_hashtags uh ON uh.uid = r.uid
        {tag_join}
        ORDER BY r.rank
    """


def _changeset_filters_sql(
    *,
    filter_dates: bool,
    filter_hashtags: bool = False,
) -> tuple[str, int]:
    n = 1
    filters: list[str] = []
    if filter_dates:
        filters.append(f"cs.created_at >= ${n}")
        n += 1
        filters.append(f"cs.created_at < ${n}")
        n += 1
    if filter_hashtags:
        filters.append(f"cs.hashtags && ${n}::TEXT[]")
        n += 1
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    return where_sql, n


def _hashtag_stats_sql(
    *,
    filter_dates: bool,
    filter_hashtags: bool,
) -> str:
    where_sql, n = _changeset_filters_sql(
        filter_dates=filter_dates,
        filter_hashtags=filter_hashtags,
    )
    hashtag_param = f"${3 if filter_dates else 1}" if filter_hashtags else None
    hashtag_value_filter = f"AND ht.hashtag = ANY({hashtag_param}::TEXT[])" if hashtag_param else ""
    limit_param = f"${n}"
    offset_param = f"${n + 1}"
    map_changes = _map_changes_expr()
    return f"""
        WITH hashtag_scope AS (
            SELECT
                ht.hashtag,
                st.uid,
                st.changeset_id,
                ({map_changes}) AS map_changes
            FROM changesets cs
            JOIN changeset_stats st ON st.changeset_id = cs.changeset_id
            CROSS JOIN LATERAL UNNEST(cs.hashtags) AS ht(hashtag)
            {where_sql}
            {hashtag_value_filter}
        ),
        hashtag_totals AS (
            SELECT
                hashtag,
                COUNT(DISTINCT changeset_id) AS changesets,
                COUNT(DISTINCT uid) AS users,
                COALESCE(SUM(map_changes), 0) AS map_changes
            FROM hashtag_scope
            GROUP BY hashtag
        )
        SELECT
            hashtag,
            changesets,
            users,
            map_changes,
            ROW_NUMBER() OVER (ORDER BY map_changes DESC, hashtag ASC) AS rank
        FROM hashtag_totals
        ORDER BY map_changes DESC, hashtag ASC
        LIMIT {limit_param} OFFSET {offset_param}
    """


def _hashtag_trends_sql(*, filter_hashtags: bool) -> str:
    where_sql, n = _changeset_filters_sql(
        filter_dates=True,
        filter_hashtags=filter_hashtags,
    )
    hashtag_param = "$3" if filter_hashtags else None
    hashtag_value_filter = f"AND ht.hashtag = ANY({hashtag_param}::TEXT[])" if hashtag_param else ""
    interval_param = f"${n}"
    limit_param = f"${n + 1}"
    offset_param = f"${n + 2}"
    map_changes = _map_changes_expr()
    return f"""
        SELECT
            DATE_TRUNC({interval_param}, cs.created_at) AS period_start,
            ht.hashtag,
            COUNT(DISTINCT st.changeset_id) AS changesets,
            COUNT(DISTINCT st.uid) AS users,
            COALESCE(SUM({map_changes}), 0) AS map_changes
        FROM changesets cs
        JOIN changeset_stats st ON st.changeset_id = cs.changeset_id
        CROSS JOIN LATERAL UNNEST(cs.hashtags) AS ht(hashtag)
        {where_sql}
        {hashtag_value_filter}
        GROUP BY period_start, ht.hashtag
        ORDER BY period_start ASC, map_changes DESC, ht.hashtag ASC
        LIMIT {limit_param} OFFSET {offset_param}
    """


def _editor_stats_sql(*, filter_dates: bool, include_version: bool) -> str:
    where_sql, n = _changeset_filters_sql(filter_dates=filter_dates)
    limit_param = f"${n}"
    offset_param = f"${n + 1}"
    map_changes = _map_changes_expr()
    editor_expr = (
        "COALESCE(NULLIF(cs.editor, ''), 'unknown')"
        if include_version
        else "COALESCE(NULLIF(REGEXP_REPLACE(cs.editor, E'([ /]v?[0-9].*)$', '', 'i'), ''), 'unknown')"
    )
    return f"""
        WITH editor_scope AS (
            SELECT
                {editor_expr} AS editor,
                st.uid,
                st.changeset_id,
                ({map_changes}) AS map_changes
            FROM changesets cs
            JOIN changeset_stats st ON st.changeset_id = cs.changeset_id
            {where_sql}
        ),
        editor_totals AS (
            SELECT
                editor,
                COUNT(DISTINCT changeset_id) AS changesets,
                COUNT(DISTINCT uid) AS users,
                COALESCE(SUM(map_changes), 0) AS map_changes
            FROM editor_scope
            GROUP BY editor
        )
        SELECT
            editor,
            changesets,
            users,
            map_changes,
            ROW_NUMBER() OVER (ORDER BY map_changes DESC, editor ASC) AS rank
        FROM editor_totals
        ORDER BY map_changes DESC, editor ASC
        LIMIT {limit_param} OFFSET {offset_param}
    """


def _map_geometry_sql(mode: str | None) -> tuple[str, str]:
    if mode == GEOMETRY_MODE_GEOM:
        return (
            "ST_AsGeoJSON(ST_Centroid(cs.geom))::TEXT AS geometry",
            "cs.geom",
        )
    if mode == GEOMETRY_MODE_BBOX:
        return (
            f"{_bbox_centroid_geojson_sql()} AS geometry",
            "cs.min_lon, cs.min_lat, cs.max_lon, cs.max_lat",
        )
    return "NULL::TEXT AS geometry", ""


def _map_geometry_filter_sql(mode: str | None) -> str:
    if mode == GEOMETRY_MODE_GEOM:
        return "cs.geom IS NOT NULL"
    if mode == GEOMETRY_MODE_BBOX:
        return " AND ".join(
            [
                "cs.min_lon IS NOT NULL",
                "cs.min_lat IS NOT NULL",
                "cs.max_lon IS NOT NULL",
                "cs.max_lat IS NOT NULL",
            ]
        )
    return "FALSE"


def _map_changes_sql(
    *,
    filter_dates: bool,
    filter_hashtags: bool,
    geometry_mode: str | None,
) -> str:
    where_sql, n = _changeset_filters_sql(
        filter_dates=filter_dates,
        filter_hashtags=filter_hashtags,
    )
    limit_param = f"${n}"
    offset_param = f"${n + 1}"
    map_changes = _map_changes_expr()
    geometry_select, geometry_group = _map_geometry_sql(geometry_mode)
    geometry_group_sql = f", {geometry_group}" if geometry_group else ""
    geometry_filter = _map_geometry_filter_sql(geometry_mode)
    located_where_sql = f"{where_sql} AND {geometry_filter}" if where_sql else f"WHERE {geometry_filter}"
    return f"""
        WITH filtered_changesets AS (
            SELECT cs.*
            FROM changesets cs
            {located_where_sql}
            ORDER BY cs.created_at DESC NULLS LAST, cs.changeset_id DESC
            LIMIT {limit_param} OFFSET {offset_param}
        )
        SELECT
            cs.changeset_id,
            cs.uid,
            u.username AS name,
            cs.created_at,
            COALESCE(cs.hashtags, ARRAY[]::TEXT[]) AS hashtags,
            COALESCE(NULLIF(cs.editor, ''), 'unknown') AS editor,
            COALESCE(SUM({map_changes}), 0) AS map_changes,
            COALESCE(SUM(st.nodes_created), 0) AS nodes_create,
            COALESCE(SUM(st.nodes_modified), 0) AS nodes_modify,
            COALESCE(SUM(st.nodes_deleted), 0) AS nodes_delete,
            COALESCE(SUM(st.ways_created), 0) AS ways_create,
            COALESCE(SUM(st.ways_modified), 0) AS ways_modify,
            COALESCE(SUM(st.ways_deleted), 0) AS ways_delete,
            COALESCE(SUM(st.rels_created), 0) AS rels_create,
            COALESCE(SUM(st.rels_modified), 0) AS rels_modify,
            COALESCE(SUM(st.rels_deleted), 0) AS rels_delete,
            {geometry_select}
        FROM filtered_changesets cs
        JOIN users u ON u.uid = cs.uid
        LEFT JOIN changeset_stats st ON st.changeset_id = cs.changeset_id
        GROUP BY cs.changeset_id, cs.uid, u.username, cs.created_at, cs.hashtags, cs.editor{geometry_group_sql}
        ORDER BY cs.created_at DESC NULLS LAST, cs.changeset_id DESC
    """


async def _changesets_columns() -> set[str]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'changesets'
            """
        )
    return {row["column_name"] for row in rows}


async def _postgis_available() -> bool:
    async with get_pool().acquire() as conn:
        return bool(await conn.fetchval("SELECT COUNT(*) FROM pg_extension WHERE extname = 'postgis'"))


async def _changesets_geometry_mode() -> str | None:
    columns = await _changesets_columns()
    if "geom" in columns and await _postgis_available():
        return GEOMETRY_MODE_GEOM
    if {"min_lon", "min_lat", "max_lon", "max_lat"}.issubset(columns):
        return GEOMETRY_MODE_BBOX
    return None


async def fetch_state() -> dict[str, Any] | None:
    # last_ts/last_seq come from the worst-lagging source (slowest source bounds real freshness);
    # updated_at is the most recent heartbeat across all sources (any tick proves the worker is alive).
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT last_seq, last_ts, (SELECT MAX(updated_at) FROM state) AS updated_at
            FROM state
            ORDER BY last_ts ASC
            LIMIT 1
            """
        )
    if row is None:
        return None
    return dict(row)


async def fetch_user_stats(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    hashtag: list[str] | None = None,
    tag_mode: str = "keys",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    filter_dates = start is not None and end is not None
    filter_hashtags = bool(hashtag)
    sql = _user_stats_sql(
        filter_dates=filter_dates,
        filter_hashtags=filter_hashtags,
        tag_mode=tag_mode,
    )
    params: list[Any] = []
    if filter_dates:
        params.extend([start, end])
    if filter_hashtags:
        params.append(hashtag)
    params.extend([limit, offset])

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(row) for row in rows]


async def fetch_hashtag_stats(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    hashtag: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    filter_dates = start is not None and end is not None
    filter_hashtags = bool(hashtag)
    sql = _hashtag_stats_sql(
        filter_dates=filter_dates,
        filter_hashtags=filter_hashtags,
    )
    params: list[Any] = []
    if filter_dates:
        params.extend([start, end])
    if filter_hashtags:
        params.append(hashtag)
    params.extend([limit, offset])

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(row) for row in rows]


async def fetch_hashtag_trends(
    *,
    start: datetime,
    end: datetime,
    interval: str,
    hashtag: list[str] | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    filter_hashtags = bool(hashtag)
    sql = _hashtag_trends_sql(filter_hashtags=filter_hashtags)
    params: list[Any] = [start, end]
    if filter_hashtags:
        params.append(hashtag)
    params.extend([interval, limit, offset])

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(row) for row in rows]


async def fetch_editor_stats(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    include_version: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    filter_dates = start is not None and end is not None
    sql = _editor_stats_sql(filter_dates=filter_dates, include_version=include_version)
    params: list[Any] = []
    if filter_dates:
        params.extend([start, end])
    params.extend([limit, offset])

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(row) for row in rows]


async def fetch_map_changes(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    hashtag: list[str] | None = None,
    limit: int = 500,
    offset: int = 0,
) -> dict[str, Any]:
    filter_dates = start is not None and end is not None
    filter_hashtags = bool(hashtag)
    geometry_mode = await _changesets_geometry_mode()
    sql = _map_changes_sql(
        filter_dates=filter_dates,
        filter_hashtags=filter_hashtags,
        geometry_mode=geometry_mode,
    )
    params: list[Any] = []
    if filter_dates:
        params.extend([start, end])
    if filter_hashtags:
        params.append(hashtag)
    params.extend([limit, offset])

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)

    features = []
    for row in rows:
        data = dict(row)
        geometry = data.pop("geometry")
        features.append(
            {
                "type": "Feature",
                "geometry": json.loads(geometry) if geometry else None,
                "properties": data,
            }
        )
    return {"type": "FeatureCollection", "features": features}
