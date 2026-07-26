"""The hashtag query surface (summary, leaderboard, tags, editors, trends, map), built from the catalog
+ stats vocabulary so the CLI and API compute identically. History and recent are disjoint at the
frontier, so each function aggregates them separately and combines, keeping large hashtags fast."""

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
    """Where per-changeset rows and usernames live: `history_rel` (published rollup) serves before
    `frontier`; the recent tail comes live from `recent_stats_rel`/`recent_changesets_rel`."""

    history_rel: str
    recent_stats_rel: str
    recent_changesets_rel: str
    frontier: dt.datetime
    users_rel: str
    # When set (the attached-Postgres API path), the recent side is aggregated inside Postgres by the
    # `changeset_hashtag` index instead of scanned per-changeset. None -> local DuckDB store (columnar).
    pg_attach: str | None = None


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


def _history(s: Sources, prefixes, start, end) -> tuple[str, list[object]]:
    """The deduped history-side per-changeset SELECT (sql, params) for the hashtag scope + window."""
    return catalog.history_dedup_scope(s.history_rel, prefixes=prefixes, frontier=s.frontier, start=start, end=end)


def _recent_perchangeset(s: Sources, prefixes, start, end) -> tuple[str, list[object]]:
    """The recent-side per-changeset relation for the local DuckDB store (columnar, small tail)."""
    return catalog.recent_scope(
        s.recent_stats_rel, s.recent_changesets_rel, prefixes=prefixes, frontier=s.frontier, start=start, end=end
    )


def _recent_users(s: Sources, prefixes, start, end) -> tuple[str, list[object]]:
    """Recent side as a per-uid aggregate relation (uid, changesets, count cols): aggregated in Postgres
    on the API path, or rolled up from the store's per-changeset scan otherwise."""
    if s.pg_attach:
        return catalog.recent_user_agg(s.pg_attach, prefixes=prefixes, frontier=s.frontier, start=start, end=end), []
    rsql, rp = _recent_perchangeset(s, prefixes, start, end)
    return f"(SELECT uid, count(*) AS changesets, {_SUM_AS} FROM ({rsql}) GROUP BY uid)", rp


def _recent_leaderboard(s: Sources, prefixes, start, end) -> tuple[str, list[object]]:
    """Recent per-uid aggregate including each user's distinct editors, for the leaderboard."""
    if s.pg_attach:
        return catalog.recent_leaderboard_agg(
            s.pg_attach, prefixes=prefixes, frontier=s.frontier, start=start, end=end
        ), []
    rsql, rp = _recent_perchangeset(s, prefixes, start, end)
    return (
        f"(SELECT uid, count(*) AS changesets, {_SUM_AS}, list(DISTINCT editor) AS editors FROM ({rsql}) GROUP BY uid)",
        rp,
    )


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
    prefixes = _prefixes(hashtag)
    hsql, hp = _history(s, prefixes, start, end)
    rrel, rp = _recent_users(s, prefixes, start, end)
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
                SELECT uid, changesets, {_COLS} FROM {rrel}
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
    s: Sources,
    prefixes: list[tuple[str, str]],
    start: dt.datetime | None,
    end: dt.datetime | None,
    hist_rel: str | None = None,
) -> None:
    """In-place: attach per-user `tag_stats` (nested `{key:{value:{c,m}}}`) to the returned leaderboard
    page. History tags are unnested for the page's uids (from `hist_rel` if the caller already materialized
    the deduped history, else recomputed); recent tags come from the Postgres aggregate or the store scan."""
    for r in rows:
        r["tag_stats"] = {}
    if not rows:
        return
    uids = [r["uid"] for r in rows]
    ph = ", ".join("?" for _ in uids)
    if hist_rel is not None:
        hist_src, hp = hist_rel, []
    else:
        hsql, hp = _history(s, prefixes, start, end)
        hist_src = f"({hsql})"
    hist = (
        f"SELECT uid, t.k AS k, t.v AS v, t.c AS c, t.m AS m, t.len_m AS len_m "
        f"FROM (SELECT uid, UNNEST(tags) AS t FROM {hist_src} WHERE uid IN ({ph}))"
    )
    if s.pg_attach:
        rrel = catalog.recent_user_tags(s.pg_attach, uids, prefixes=prefixes, frontier=s.frontier, start=start, end=end)
        recent = f"SELECT uid, k, v, c, m, len_m FROM {rrel}"
        params = [*hp, *uids]
    else:
        rsql, rp = _recent_perchangeset(s, prefixes, start, end)
        recent = (
            f"SELECT uid, t.k AS k, t.v AS v, t.c AS c, t.m AS m, t.len_m AS len_m "
            f"FROM (SELECT uid, UNNEST(tags) AS t FROM ({rsql}) WHERE uid IN ({ph}))"
        )
        params = [*hp, *uids, *rp, *uids]
    tag_rows = con.execute(
        f"SELECT uid, k, v, SUM(c) AS c, SUM(m) AS m, SUM(len_m) AS len_m "
        f"FROM ({hist} UNION ALL {recent}) GROUP BY uid, k, v",
        params,
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
    """In-place: attach the distinct matching hashtags each returned user contributed under, from the
    rollup `hashtag` column (history) and the base changesets' `hashtags` list (recent)."""
    for r in rows:
        r["hashtags"] = []
    if not rows:
        return
    uids = [r["uid"] for r in rows]
    ph = ", ".join("?" for _ in uids)
    window_sql, window_params = catalog._window_clause(start, end)
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
    hist = (
        f"SELECT uid, hashtag FROM {s.history_rel} WHERE ({hist_pred}) AND created_at < ?{window_sql} AND uid IN ({ph})"
    )
    hist_params = [*prefix_params, s.frontier, *window_params, *uids]
    if s.pg_attach:
        rrel = catalog.recent_user_hashtags(
            s.pg_attach, uids, prefixes=prefixes, frontier=s.frontier, start=start, end=end
        )
        recent = f"SELECT uid, hashtag FROM {rrel}"
        params = hist_params
    else:
        recent_pred = " OR ".join("(lower(h) >= ? AND lower(h) < ?)" for _ in prefixes)
        recent = (
            f"SELECT uid, h AS hashtag FROM (SELECT uid, UNNEST(hashtags) AS h FROM {s.recent_changesets_rel} "
            f"WHERE created_at >= ?{window_sql} AND uid IN ({ph})) WHERE {recent_pred}"
        )
        params = [*hist_params, s.frontier, *window_params, *uids, *prefix_params]
    result = con.execute(
        f"SELECT uid, list(DISTINCT hashtag) AS hashtags FROM ({hist} UNION ALL {recent}) GROUP BY uid",
        params,
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
    """One page of the hashtag leaderboard as `{items, page, page_size, total, total_pages}`, ordered by
    `sort`/`order` and filtered by the optional `q` name search and [start, end) window. Per-user
    `tag_stats` is attached only for hashtags small enough to afford it; the aggregate lives on `/tags`."""
    if sort not in LEADERBOARD_SORTS:
        raise ValueError(f"sort must be one of {tuple(LEADERBOARD_SORTS)}")
    if order not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    prefixes = _prefixes(hashtag)
    hsql, hp = _history(s, prefixes, start, end)
    # Materialize the deduped history once; _q_lb and the per-user tag attach both read it (avoids
    # re-scanning the rollup three times for a mega-hashtag page).
    con.execute(f"CREATE OR REPLACE TEMP TABLE _hist_cs AS SELECT * FROM ({hsql})", hp)
    rrel, rp = _recent_leaderboard(s, prefixes, start, end)
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE _q_lb AS "
        f"SELECT uid, count(*) AS changesets, {_SUM_AS}, list(DISTINCT editor) AS editors FROM _hist_cs GROUP BY uid"
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
            SELECT uid, changesets, {_COLS}, editors FROM {rrel}
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
        s.history_rel, prefixes=prefixes, frontier=s.frontier, start=start, end=end
    )
    count_row = con.execute(count_sql, count_params).fetchone()
    history_rows = count_row[0] if count_row else 0
    if history_rows <= _MAX_TAG_ROWS:
        _attach_user_tags(con, rows, s, prefixes, start, end, hist_rel="_hist_cs")
    else:
        for r in rows:
            r["tag_stats"] = {}
    _attach_user_hashtags(con, rows, s, prefixes, start, end)
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
    prefixes = _prefixes(hashtag)
    hsql, hp = _history(s, prefixes, start, end)
    per_tag = (
        "SELECT t.k AS k, t.v AS v, SUM(t.c) AS creates, SUM(t.m) AS modifies, SUM(t.len_m) AS length_m "
        "FROM (SELECT UNNEST(tags) AS t FROM {rel}) GROUP BY t.k, t.v"
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE _q_tg AS {per_tag.format(rel=f'({hsql})')}", hp)
    if s.pg_attach:
        rrel = catalog.recent_tag_agg(s.pg_attach, prefixes=prefixes, frontier=s.frontier, start=start, end=end)
        recent_sel, rp = f"SELECT k, v, creates, modifies, length_m FROM {rrel}", []
    else:
        rsql, rp = _recent_perchangeset(s, prefixes, start, end)
        recent_sel = per_tag.format(rel=f"({rsql})")
    res = con.execute(
        f"""
        SELECT k AS tag_key, v AS tag_value, SUM(creates) AS creates, SUM(modifies) AS modifies,
               SUM(length_m) AS length_m
        FROM (SELECT k, v, creates, modifies, length_m FROM _q_tg UNION ALL {recent_sel})
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
    prefixes = _prefixes(hashtag)
    hsql, hp = _history(s, prefixes, start, end)
    per_eu = (
        "SELECT COALESCE(NULLIF(editor, ''), 'unknown') AS editor, uid, count(*) AS cs, "
        f"{map_changes_sum()} FROM {{rel}} GROUP BY 1, uid"
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE _q_ed AS {per_eu.format(rel=f'({hsql})')}", hp)
    if s.pg_attach:
        rrel = catalog.recent_editor_agg(s.pg_attach, prefixes=prefixes, frontier=s.frontier, start=start, end=end)
        recent_sel, rp = f"SELECT editor, uid, cs, map_changes FROM {rrel}", []
    else:
        rsql, rp = _recent_perchangeset(s, prefixes, start, end)
        recent_sel = per_eu.format(rel=f"({rsql})")
    res = con.execute(
        f"""
        WITH eu AS (
            SELECT editor, uid, cs, map_changes FROM _q_ed UNION ALL {recent_sel}
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
    """Matched hashtags with their distinct contributors and total edits, most contributors first:
    `[{hashtag, users, edits}]`. Optional [start, end) window."""
    prefixes = _prefixes(hashtag)
    window_sql, window_params = catalog._window_clause(start, end)
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
    hist_part = (
        f"SELECT hashtag, uid, {map_changes_expr()} AS mc FROM {s.history_rel} "
        f"WHERE ({hist_pred}) AND created_at < ?{window_sql}"
    )
    hist_params = [*prefix_params, s.frontier, *window_params]
    if s.pg_attach:
        rrel = catalog.recent_hashtag_agg(s.pg_attach, prefixes=prefixes, frontier=s.frontier, start=start, end=end)
        recent_part = f"SELECT hashtag, uid, mc FROM {rrel}"
        params = [*hist_params, limit]
    else:
        recent_pred = " OR ".join("(lower(h) >= ? AND lower(h) < ?)" for _ in prefixes)
        recent_part = (
            f"SELECT h AS hashtag, uid, mc FROM (SELECT c.uid, UNNEST(c.hashtags) AS h, cs.mc AS mc "
            f"FROM {s.recent_changesets_rel} c JOIN (SELECT changeset_id, {map_changes_sum(alias='mc')} "
            f"FROM {s.recent_stats_rel} GROUP BY changeset_id) cs USING (changeset_id) "
            f"WHERE c.created_at >= ?{window_sql}) WHERE {recent_pred}"
        )
        params = [*hist_params, s.frontier, *window_params, *prefix_params, limit]
    res = con.execute(
        f"""
        SELECT hashtag, count(DISTINCT uid) AS users, COALESCE(SUM(mc), 0) AS edits
        FROM ({hist_part} UNION ALL {recent_part})
        GROUP BY hashtag ORDER BY users DESC, hashtag ASC LIMIT ?
        """,
        params,
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
    prefixes = _prefixes(hashtag)
    hsql, hp = _history(s, prefixes, start, end)
    bucket = f"CAST(date_trunc('{interval}', created_at) AS DATE)::VARCHAR"
    per_bucket = (
        f"SELECT {bucket} AS bucket, count(*) AS changesets, count(DISTINCT uid) AS users, {map_changes_sum()} "
        "FROM {rel} GROUP BY 1"
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE _q_tr AS {per_bucket.format(rel=f'({hsql})')}", hp)
    if s.pg_attach:
        rrel = catalog.recent_bucket_agg(
            s.pg_attach, interval, prefixes=prefixes, frontier=s.frontier, start=start, end=end
        )
        recent_sel, rp = f"SELECT bucket, changesets, users, map_changes FROM {rrel}", []
    else:
        rsql, rp = _recent_perchangeset(s, prefixes, start, end)
        recent_sel = per_bucket.format(rel=f"({rsql})")
    res = con.execute(
        f"""
        SELECT bucket, SUM(changesets) AS changesets, SUM(users) AS users, SUM(map_changes) AS map_changes
        FROM (SELECT bucket, changesets, users, map_changes FROM _q_tr
              UNION ALL {recent_sel})
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
    prefixes = _prefixes(hashtag)
    if s.pg_attach:
        window_sql, window_params = catalog._window_clause(start, end)
        prefix_params = [bound for pair in prefixes for bound in pair]
        hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
        hist = (
            f"SELECT changeset_id, uid, lon, lat FROM {s.history_rel} "
            f"WHERE ({hist_pred}) AND created_at < ?{window_sql} AND lon IS NOT NULL"
        )
        rrel = catalog.recent_map_agg(s.pg_attach, prefixes=prefixes, frontier=s.frontier, start=start, end=end)
        sql = (
            f"SELECT DISTINCT ON (changeset_id) changeset_id, uid, lon, lat "
            f"FROM ({hist} UNION ALL SELECT changeset_id, uid, lon, lat FROM {rrel})"
        )
        res = con.execute(f"{sql} LIMIT ?", [*prefix_params, s.frontier, *window_params, limit])
        return _rows(res)
    sql, params = map_scope(
        s.history_rel, s.recent_changesets_rel, prefixes=prefixes, frontier=s.frontier, start=start, end=end
    )
    res = con.execute(f"{sql} LIMIT ?", [*params, limit])
    return _rows(res)
