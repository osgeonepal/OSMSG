from datetime import datetime
from typing import Any

from .db import get_pool


def _user_stats_sql(*, filter_dates: bool, filter_hashtags: bool, include_tags: bool) -> str:
    n = 1
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

    tag_ctes = (
        """,
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
        if include_tags
        else ""
    )

    tag_select = "tpu.tag_stats" if include_tags else "NULL::jsonb AS tag_stats"
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
    tags: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    filter_dates = start is not None and end is not None
    filter_hashtags = bool(hashtag)
    sql = _user_stats_sql(filter_dates=filter_dates, filter_hashtags=filter_hashtags, include_tags=tags)
    params: list[Any] = []
    if filter_dates:
        params.extend([start, end])
    if filter_hashtags:
        params.append(hashtag)
    params.extend([limit, offset])

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(row) for row in rows]
