"""The hashtag query surface: summary, leaderboard, tag breakdown, editors, trends, map. Built once here
from the catalog + stats vocabulary so the CLI and API compute identically. The caller supplies a DuckDB
connection with the sources reachable (local tables, an attached Postgres, or read_parquet('hf://...')).

Performance shape (load-bearing): history and recent are DISJOINT at the frontier, so instead of
DISTINCT ON over their UNION (which makes DuckDB materialize the whole multi-million-row history
intermediate for a big hashtag), each function aggregates the deduped history and the recent tail
SEPARATELY to a small per-grain result, then combines those. The history dedup streams; only the small
aggregate is materialized. This keeps even the largest hashtags fast when the engine has room to hold
the dedup hash table (see OSMSG_DUCKDB_MEMORY_LIMIT).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import duckdb

from . import catalog
from .catalog import map_scope
from .db.queries import _tags_to_nested
from .stats import COUNT_COLS, map_changes_expr, map_changes_sum, prefix_upper_bound, sum_cols


@dataclass(frozen=True)
class Sources:
    """Where the per-changeset rows and usernames live. `history_rel` is the published rollup relation
    (a table name or `read_parquet(...)`) serving everything before `frontier`; the recent tail from the
    frontier on is derived on the fly from the base tables `recent_stats_rel` (changeset_stats) and
    `recent_changesets_rel` (changesets), so it is always as fresh as the store. `users_rel` maps
    uid -> username."""

    history_rel: str
    recent_stats_rel: str
    recent_changesets_rel: str
    frontier: dt.datetime
    users_rel: str


def _rows(result) -> list[dict[str, Any]]:
    cols = [d[0] for d in result.description]
    return [dict(zip(cols, r, strict=True)) for r in result.fetchall()]


def _prefixes(hashtag: str | list[str]) -> list[tuple[str, str]]:
    """Normalize one hashtag or many into deduped `(lo, hi)` prefix-range pairs, case-insensitive and
    order-preserving. Each hashtag matches as a prefix; the scope is the union across them."""
    tags = [hashtag] if isinstance(hashtag, str) else list(hashtag)
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for h in tags:
        lo = "#" + h.strip().lower().lstrip("#")
        if lo == "#" or lo in seen:
            continue
        seen.add(lo)
        pairs.append((lo, prefix_upper_bound(lo)))
    if not pairs:
        raise ValueError("at least one non-empty hashtag is required")
    return pairs


def _sides(
    hashtag: str | list[str], s: Sources, start: dt.datetime | None, end: dt.datetime | None
) -> tuple[tuple[str, list[object]], tuple[str, list[object]]]:
    """The deduped history-side and recent-side SELECTs (sql, params) for the hashtag scope + window."""
    prefixes = _prefixes(hashtag)
    hist = catalog.history_dedup_scope(s.history_rel, prefixes=prefixes, frontier=s.frontier, start=start, end=end)
    rec = catalog.recent_scope(
        s.recent_stats_rel, s.recent_changesets_rel, prefixes=prefixes, frontier=s.frontier, start=start, end=end
    )
    return hist, rec


_SUM_AS = ", ".join(f"SUM({c}) AS {c}" for c in COUNT_COLS)
_COLS = ", ".join(COUNT_COLS)


def summary(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> dict[str, Any]:
    """Totals for the hashtag: distinct users, changesets, the full element breakdown. Optional [start,
    end) window. Aggregates each side per user, then combines (disjoint by frontier -> additive counts;
    distinct users is the count of combined per-user rows)."""
    (hsql, hp), (rsql, rp) = _sides(hashtag, s, start, end)
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE _q_hu AS "
        f"SELECT uid, count(*) AS changesets, {_SUM_AS} FROM ({hsql}) GROUP BY uid",
        hp,
    )
    res = con.execute(
        f"""
        WITH combined AS (
            SELECT uid, SUM(changesets) AS changesets, {_SUM_AS} FROM (
                SELECT uid, changesets, {_COLS} FROM _q_hu
                UNION ALL
                SELECT uid, count(*) AS changesets, {_SUM_AS} FROM ({rsql}) GROUP BY uid
            ) GROUP BY uid
        )
        SELECT count(*) AS users, COALESCE(SUM(changesets), 0) AS changesets,
               {sum_cols()}, {map_changes_sum()} FROM combined
        """,
        rp,
    )
    return _rows(res)[0]


# Per-user tag breakdown requires deduping and unnesting every matching changeset; above this many
# history rows it is skipped so a mega-hashtag leaderboard stays fast (its tag_stats come back empty and
# the aggregate tag breakdown is served by the /tags endpoint instead).
_MAX_TAG_ROWS = 1_500_000


def _attach_user_tags(
    con: duckdb.DuckDBPyConnection,
    rows: list[dict[str, Any]],
    hsql: str,
    hp: list[object],
    rsql: str,
    rp: list[object],
) -> None:
    """In-place: attach per-user `tag_stats` (nested `{key: {value: {c, m}}}`) to leaderboard rows,
    aggregating the native `tags` lists for just the returned page of users. Re-runs the deduped history
    and recent SELECTs filtered to those uids (cheap for the small/medium hashtags this runs for). Sets an
    empty `tag_stats` on every row when the page is empty."""
    for r in rows:
        r["tag_stats"] = {}
    if not rows:
        return
    uids = [r["uid"] for r in rows]
    ph = ", ".join("?" for _ in uids)
    tag_rows = con.execute(
        f"""
        SELECT uid, t.k AS k, t.v AS v, SUM(t.c) AS c, SUM(t.m) AS m, SUM(t.len_m) AS len_m FROM (
            SELECT uid, UNNEST(tags) AS t FROM ({hsql}) WHERE uid IN ({ph})
            UNION ALL
            SELECT uid, UNNEST(tags) AS t FROM {rsql} WHERE uid IN ({ph})
        ) GROUP BY uid, t.k, t.v
        """,
        [*hp, *uids, *rp, *uids],
    ).fetchall()
    per_uid: dict[Any, list[dict[str, Any]]] = {}
    for uid, k, v, c, m, len_m in tag_rows:
        per_uid.setdefault(uid, []).append({"k": k, "v": v, "c": c, "m": m, "len_m": len_m})
    for r in rows:
        if per_uid.get(r["uid"]):
            r["tag_stats"] = _tags_to_nested(per_uid[r["uid"]])


def _attach_user_hashtags(
    con: duckdb.DuckDBPyConnection,
    rows: list[dict[str, Any]],
    s: Sources,
    prefixes: list[tuple[str, str]],
    start: dt.datetime | None,
    end: dt.datetime | None,
) -> None:
    """In-place: attach the distinct hashtags each returned user contributed under (matching the queried
    prefixes), for the modal's per-user hashtag grid. History hashtags come straight from the rollup's
    `hashtag` column; recent from the base changesets' `hashtags` list. Bounded to the returned page's
    uids and hashtag-range-pruned, so it stays cheap even for a mega-hashtag. Empty list when unmatched."""
    for r in rows:
        r["hashtags"] = []
    if not rows:
        return
    uids = [r["uid"] for r in rows]
    ph = ", ".join("?" for _ in uids)
    window_sql, window_params = catalog._window_clause(start, end)
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
    recent_pred = " OR ".join("(lower(h) >= ? AND lower(h) < ?)" for _ in prefixes)
    result = con.execute(
        f"""
        SELECT uid, list(DISTINCT hashtag) AS hashtags FROM (
            SELECT uid, hashtag FROM {s.history_rel}
                WHERE ({hist_pred}) AND created_at < ?{window_sql} AND uid IN ({ph})
            UNION ALL
            SELECT uid, h AS hashtag FROM (
                SELECT uid, UNNEST(hashtags) AS h FROM {s.recent_changesets_rel}
                    WHERE created_at >= ?{window_sql} AND uid IN ({ph})
            ) WHERE {recent_pred}
        ) GROUP BY uid
        """,
        [
            *prefix_params,
            s.frontier,
            *window_params,
            *uids,
            s.frontier,
            *window_params,
            *uids,
            *prefix_params,
        ],
    ).fetchall()
    by_uid = {uid: list(hashtags or []) for uid, hashtags in result}
    for r in rows:
        r["hashtags"] = by_uid.get(r["uid"], [])


DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

# Sortable leaderboard columns -> the SQL expression to order by, over the materialized per-user
# aggregate. `created`/`modified`/`deleted` fold the element columns the way the table's columns show.
LEADERBOARD_SORTS = {
    "map_changes": "map_changes",
    "changesets": "changesets",
    "created": "(nodes_created + ways_created + rels_created)",
    "modified": "(nodes_modified + ways_modified + rels_modified)",
    "deleted": "(nodes_deleted + ways_deleted + rels_deleted)",
    "name": "lower(name)",
}


def leaderboard(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort: str = "map_changes",
    order: str = "desc",
    q: str | None = None,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> dict[str, Any]:
    """One page of the hashtag leaderboard as a pagination envelope
    `{items, page, page_size, total, total_pages}`. Users are aggregated once (deduped history combined
    with the recent tail) into a small per-user table; `total` is the distinct users matching the
    optional `q` name search, and `items` is the requested page ordered by `sort`/`order`. Each item
    carries the full element breakdown, editors, `rank` (position in the sorted result), and, for
    hashtags small enough to afford it, a per-user `tag_stats` breakdown (empty otherwise; the aggregate
    lives on `/tags`). Optional [start, end) window. `sort` must be in `LEADERBOARD_SORTS`, `order` in
    {asc, desc}; both are validated."""
    if sort not in LEADERBOARD_SORTS:
        raise ValueError(f"sort must be one of {tuple(LEADERBOARD_SORTS)}")
    if order not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    (hsql, hp), (rsql, rp) = _sides(hashtag, s, start, end)
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE _q_lb AS "
        f"SELECT uid, count(*) AS changesets, {_SUM_AS}, list(DISTINCT editor) AS editors FROM ({hsql}) GROUP BY uid",
        hp,
    )
    search_pred, search_params = "", []
    if q:
        search_pred = " WHERE lower(name) LIKE ?"
        search_params = [f"%{q.strip().lower()}%"]
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _lb_agg AS
        WITH pu AS (
            SELECT uid, changesets, {_COLS}, editors FROM _q_lb
            UNION ALL
            SELECT uid, count(*) AS changesets, {_SUM_AS}, list(DISTINCT editor) AS editors
            FROM ({rsql}) GROUP BY uid
        ),
        c AS (
            SELECT uid, SUM(changesets) AS changesets, {_SUM_AS},
                   list_distinct(flatten(list(editors))) AS editors
            FROM pu GROUP BY uid
        )
        SELECT c.uid, COALESCE(u.username, 'user ' || c.uid) AS name, c.changesets, {_COLS},
               {map_changes_expr("c")} AS map_changes, c.editors AS editors
        FROM c LEFT JOIN {s.users_rel} u USING (uid){search_pred}
        """,
        [*rp, *search_params],
    )
    total_row = con.execute("SELECT count(*) FROM _lb_agg").fetchone()
    total = total_row[0] if total_row else 0
    offset = (page - 1) * page_size
    rows = _rows(
        con.execute(
            f"SELECT * FROM _lb_agg ORDER BY {LEADERBOARD_SORTS[sort]} {order.upper()} NULLS LAST, uid ASC "
            f"LIMIT ? OFFSET ?",
            [page_size, offset],
        )
    )
    for i, r in enumerate(rows):
        r["rank"] = offset + i + 1
    count_sql, count_params = catalog.history_scope_count(
        s.history_rel, prefixes=_prefixes(hashtag), frontier=s.frontier, start=start, end=end
    )
    count_row = con.execute(count_sql, count_params).fetchone()
    history_rows = count_row[0] if count_row else 0
    if history_rows <= _MAX_TAG_ROWS:
        _attach_user_tags(con, rows, hsql, hp, rsql, rp)
    else:
        for r in rows:
            r["tag_stats"] = {}
    _attach_user_hashtags(con, rows, s, _prefixes(hashtag), start, end)
    return {
        "items": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
    }


def tags(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    limit: int = 100,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Tag key/value breakdown (creates/modifies/length) over the hashtag's deduped changesets. Optional
    [start, end) window."""
    (hsql, hp), (rsql, rp) = _sides(hashtag, s, start, end)
    per_tag = (
        "SELECT t.k AS k, t.v AS v, SUM(t.c) AS creates, SUM(t.m) AS modifies, SUM(t.len_m) AS length_m "
        "FROM (SELECT UNNEST(tags) AS t FROM {rel}) GROUP BY t.k, t.v"
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE _q_tg AS {per_tag.format(rel=f'({hsql})')}", hp)
    res = con.execute(
        f"""
        SELECT k AS tag_key, v AS tag_value, SUM(creates) AS creates, SUM(modifies) AS modifies,
               SUM(length_m) AS length_m
        FROM (SELECT k, v, creates, modifies, length_m FROM _q_tg UNION ALL {per_tag.format(rel=f"({rsql})")})
        GROUP BY k, v ORDER BY creates DESC LIMIT ?
        """,
        [*rp, limit],
    )
    return _rows(res)


def editors(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Editor breakdown for the hashtag: changesets, distinct users, and map_changes per editor. Optional
    [start, end) window. Aggregated per (editor, uid) on each side so distinct users are exact across the
    frontier."""
    (hsql, hp), (rsql, rp) = _sides(hashtag, s, start, end)
    per_eu = (
        "SELECT COALESCE(NULLIF(editor, ''), 'unknown') AS editor, uid, count(*) AS cs, "
        f"{map_changes_sum()} FROM {{rel}} GROUP BY 1, uid"
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE _q_ed AS {per_eu.format(rel=f'({hsql})')}", hp)
    res = con.execute(
        f"""
        WITH eu AS (
            SELECT editor, uid, cs, map_changes FROM _q_ed UNION ALL {per_eu.format(rel=f"({rsql})")}
        ),
        c AS (SELECT editor, uid, SUM(cs) AS cs, SUM(map_changes) AS map_changes FROM eu GROUP BY editor, uid)
        SELECT editor, SUM(cs) AS changesets, count(*) AS users, SUM(map_changes) AS map_changes
        FROM c GROUP BY editor ORDER BY map_changes DESC
        """,
        rp,
    )
    return _rows(res)


def hashtags(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    limit: int = 15,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Trending hashtags within the queried scope: the matched hashtags with their distinct contributors
    and total map changes (edits). History from the rollup `hashtag` column + count columns, recent from
    the base changesets' `hashtags` list joined to changeset_stats. Optional [start, end) window. Returns
    `[{hashtag, users, edits}]`, most contributors first."""
    prefixes = _prefixes(hashtag)
    window_sql, window_params = catalog._window_clause(start, end)
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
    recent_pred = " OR ".join("(lower(h) >= ? AND lower(h) < ?)" for _ in prefixes)
    res = con.execute(
        f"""
        SELECT hashtag, count(DISTINCT uid) AS users, COALESCE(SUM(mc), 0) AS edits FROM (
            SELECT hashtag, uid, {map_changes_expr()} AS mc FROM {s.history_rel}
                WHERE ({hist_pred}) AND created_at < ?{window_sql}
            UNION ALL
            SELECT h AS hashtag, uid, mc FROM (
                SELECT c.uid, UNNEST(c.hashtags) AS h, cs.mc AS mc
                FROM {s.recent_changesets_rel} c
                JOIN (
                    SELECT changeset_id, {map_changes_sum(alias="mc")}
                    FROM {s.recent_stats_rel} GROUP BY changeset_id
                ) cs USING (changeset_id)
                WHERE c.created_at >= ?{window_sql}
            ) WHERE {recent_pred}
        ) GROUP BY hashtag ORDER BY users DESC, hashtag ASC LIMIT ?
        """,
        [*prefix_params, s.frontier, *window_params, s.frontier, *window_params, *prefix_params, limit],
    )
    return _rows(res)


TREND_INTERVALS = ("day", "week", "month")


def trends(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    interval: str = "day",
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Per-bucket activity for the hashtag: changesets, distinct users, and map_changes per day, week, or
    month (UTC). Optional [start, end) window. Buckets are disjoint at the frontier, so per-side bucket
    aggregates combine additively."""
    if interval not in TREND_INTERVALS:
        raise ValueError(f"interval must be one of {TREND_INTERVALS}")
    (hsql, hp), (rsql, rp) = _sides(hashtag, s, start, end)
    bucket = f"CAST(date_trunc('{interval}', created_at) AS DATE)::VARCHAR"
    per_bucket = (
        f"SELECT {bucket} AS bucket, count(*) AS changesets, count(DISTINCT uid) AS users, {map_changes_sum()} "
        "FROM {rel} GROUP BY 1"
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE _q_tr AS {per_bucket.format(rel=f'({hsql})')}", hp)
    res = con.execute(
        f"""
        SELECT bucket, SUM(changesets) AS changesets, SUM(users) AS users, SUM(map_changes) AS map_changes
        FROM (SELECT bucket, changesets, users, map_changes FROM _q_tr
              UNION ALL {per_bucket.format(rel=f"({rsql})")})
        GROUP BY bucket ORDER BY bucket
        """,
        rp,
    )
    return _rows(res)


def map_points(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    limit: int = 2000,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Changeset centroids `(changeset_id, uid, lon, lat)` for the hashtag union, up to `limit`, for a
    map. Optional [start, end) window. History centroids come from the rollup, recent from the base
    changesets bbox; the rollup must carry `lon`/`lat` (published rollups built after the map change do)."""
    sql, params = map_scope(
        s.history_rel, s.recent_changesets_rel, prefixes=_prefixes(hashtag), frontier=s.frontier, start=start, end=end
    )
    res = con.execute(f"{sql} LIMIT ?", [*params, limit])
    return _rows(res)
