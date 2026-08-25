"""Assemble query sources into one relation so `stats.py` runs a single query wherever data lives: history
(< frontier) from the `hashtag_changeset` rollup, the recent tail from the base tables (a local DuckDB store
or attached Postgres, hashtag-filtered first), unioned and split at the frontier. The query runs in DuckDB.
"""

from __future__ import annotations

import datetime as dt

from .stats import COUNT_COLS, MAP_CHANGES_COLS

_PROJECTION = f"changeset_id, uid, editor, created_at, {', '.join(COUNT_COLS)}, tags"


def _pg_ts(value: dt.datetime) -> str:
    return "TIMESTAMPTZ '" + value.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S%z") + "'"


def _pg_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _pg_query(attach: str, inner: str) -> str:
    return f"postgres_query('{attach}', '{inner.replace(chr(39), chr(39) * 2)}')"


# Recent side aggregated in Postgres via the changeset_hashtag prefix index; each returns an inlined
# postgres_query relation (no bind params), DISTINCT-deduped so a multi-tag changeset counts once.

_PSUM = ", ".join(f"sum(s.{c}) AS {c}" for c in COUNT_COLS)
_OSUM = ", ".join(f"sum({c}) AS {c}" for c in COUNT_COLS)
_MC = " + ".join(f"s.{c}" for c in MAP_CHANGES_COLS)


def _pg_matched(prefixes, frontier, start, end, hashtag_table="changeset_hashtag", carry_created_at=False) -> str:
    if not prefixes:
        raise ValueError("prefixes must be non-empty")
    ranges = " OR ".join(
        f'(hashtag COLLATE "C" >= {_pg_str(lo)} AND hashtag COLLATE "C" < {_pg_str(hi)})' for lo, hi in prefixes
    )
    bounds = [f"created_at >= {_pg_ts(frontier)}"]
    if start is not None:
        bounds.append(f"created_at >= {_pg_ts(start)}")
    if end is not None:
        bounds.append(f"created_at < {_pg_ts(end)}")
    where = f"({ranges}) AND {' AND '.join(bounds)}"
    if carry_created_at:
        # A changeset's rows all carry its created_at, so min() is that value: lets trends bucket without
        # a second scan of `changesets` just for the timestamp.
        return (
            f"SELECT changeset_id, min(created_at) AS created_at "
            f"FROM {hashtag_table} WHERE {where} GROUP BY changeset_id"
        )
    return f"SELECT DISTINCT changeset_id FROM {hashtag_table} WHERE {where}"


def _pg_perchangeset(prefixes, frontier, start, end, extra: str = "") -> str:
    """CTEs `m` (matched changeset ids) and `pc` (one row per changeset: uid, summed counts{extra}),
    the shared basis every recent aggregate groups from."""
    return (
        f"WITH m AS ({_pg_matched(prefixes, frontier, start, end)}), "
        f"pc AS (SELECT s.changeset_id, max(s.uid) AS uid{extra}, {_PSUM} "
        f"FROM m JOIN changeset_stats s USING (changeset_id) GROUP BY s.changeset_id)"
    )


def recent_user_agg(attach, *, prefixes, frontier, start=None, end=None) -> str:
    """Per-uid recent aggregate: changesets + summed counts. Powers summary and leaderboard."""
    inner = (
        f"{_pg_perchangeset(prefixes, frontier, start, end)} "
        f"SELECT uid, count(*) AS changesets, {_OSUM} FROM pc GROUP BY uid"
    )
    return _pg_query(attach, inner)


def recent_tag_agg(attach, *, prefixes, frontier, start=None, end=None) -> str:
    """Per (key, value) recent tag breakdown: creates, modifies, length. Powers the tags endpoint."""
    m = _pg_matched(prefixes, frontier, start, end)
    inner = (
        f"WITH m AS ({m}) "
        f"SELECT (t).k AS k, (t).v AS v, sum((t).c) AS creates, sum((t).m) AS modifies, "
        f"sum((t).l) AS length_m FROM m JOIN changeset_stats s USING (changeset_id), unnest(s.tags) AS t "
        f"GROUP BY (t).k, (t).v"
    )
    return _pg_query(attach, inner)


def recent_editor_agg(attach, *, prefixes, frontier, start=None, end=None) -> str:
    """Per (editor, uid) recent aggregate: changesets (cs), map_changes. Powers the editors endpoint."""
    basis = _pg_perchangeset(prefixes, frontier, start, end, extra=f", max(c.editor) AS editor, sum({_MC}) AS mc")
    basis = basis.replace(
        "FROM m JOIN changeset_stats s USING (changeset_id)",
        "FROM m JOIN changeset_stats s USING (changeset_id) JOIN changesets c USING (changeset_id)",
    )
    inner = (
        f"{basis} SELECT COALESCE(NULLIF(editor, ''), 'unknown') AS editor, uid, count(*) AS cs, "
        f"sum(mc) AS map_changes FROM pc GROUP BY 1, uid"
    )
    return _pg_query(attach, inner)


def recent_bucket_agg(attach, interval: str, *, prefixes, frontier, start=None, end=None) -> str:
    """Per time-bucket recent aggregate: changesets, distinct users, map_changes. Powers the trends
    endpoint. `created_at` comes from `changeset_hashtag` (carried in the matched CTE), so this joins only
    changeset_stats, no second scan of changesets. Buckets are disjoint across the frontier -> additive."""
    m = _pg_matched(prefixes, frontier, start, end, carry_created_at=True)
    inner = (
        f"WITH m AS ({m}), "
        f"pc AS (SELECT s.changeset_id, max(s.uid) AS uid, m.created_at AS created_at, sum({_MC}) AS mc "
        f"FROM m JOIN changeset_stats s USING (changeset_id) GROUP BY s.changeset_id, m.created_at) "
        f"SELECT to_char(date_trunc({_pg_str(interval)}, created_at), 'YYYY-MM-DD') AS bucket, "
        f"count(*) AS changesets, count(DISTINCT uid) AS users, sum(mc) AS map_changes FROM pc GROUP BY 1"
    )
    return _pg_query(attach, inner)


def recent_hashtag_agg(attach, *, prefixes, frontier, start=None, end=None) -> str:
    """Per (hashtag, uid) recent aggregate: changesets, map_changes. Powers the trending-hashtags endpoint
    (each matched hashtag attributes each of its changesets, so the sums are per hashtag, not deduped)."""
    m = _pg_matched(prefixes, frontier, start, end)
    ranges = " OR ".join(
        f'(ch.hashtag COLLATE "C" >= {_pg_str(lo)} AND ch.hashtag COLLATE "C" < {_pg_str(hi)})' for lo, hi in prefixes
    )
    inner = (
        f"WITH m AS ({m}), pc AS (SELECT s.changeset_id, max(s.uid) AS uid, sum({_MC}) AS mc "
        f"FROM m JOIN changeset_stats s USING (changeset_id) GROUP BY s.changeset_id) "
        f"SELECT ch.hashtag AS hashtag, pc.uid AS uid, count(*) AS changesets, sum(pc.mc) AS mc "
        f"FROM pc JOIN changeset_hashtag ch USING (changeset_id) WHERE {ranges} GROUP BY ch.hashtag, pc.uid"
    )
    return _pg_query(attach, inner)


def recent_cooccur_agg(attach, *, prefixes, frontier, start=None, end=None) -> str:
    """Per (hashtag, uid) recent aggregate over ALL hashtags carried by the matched changesets, so the
    panel can show which other hashtags and projects were used alongside the search (co-occurring).
    Same as `recent_hashtag_agg` but without narrowing the joined hashtags to the searched ranges."""
    m = _pg_matched(prefixes, frontier, start, end)
    inner = (
        f"WITH m AS ({m}), pc AS (SELECT s.changeset_id, max(s.uid) AS uid, sum({_MC}) AS mc "
        f"FROM m JOIN changeset_stats s USING (changeset_id) GROUP BY s.changeset_id) "
        f"SELECT ch.hashtag AS hashtag, pc.uid AS uid, count(*) AS changesets, sum(pc.mc) AS mc "
        f"FROM pc JOIN changeset_hashtag ch USING (changeset_id) GROUP BY ch.hashtag, pc.uid"
    )
    return _pg_query(attach, inner)


def recent_map_agg(attach, *, prefixes, frontier, start=None, end=None) -> str:
    """Per-changeset recent centroids (changeset_id, uid, lon, lat) from the bbox midpoint. Powers the map."""
    m = _pg_matched(prefixes, frontier, start, end)
    inner = (
        f"WITH m AS ({m}) SELECT c.changeset_id, c.uid, (c.min_lon + c.max_lon) / 2.0 AS lon, "
        f"(c.min_lat + c.max_lat) / 2.0 AS lat FROM m JOIN changesets c USING (changeset_id) "
        f"WHERE c.min_lon IS NOT NULL"
    )
    return _pg_query(attach, inner)


# Whole-OSM (no-hashtag) aggregates over a recent window, from Postgres.


def _pg_global_window(start: dt.datetime, end: dt.datetime) -> str:
    if start is None or end is None:
        raise ValueError("global scope requires a bounded [start, end) window")
    return f"c.created_at >= {_pg_ts(start)} AND c.created_at < {_pg_ts(end)}"


def _pg_uid_list(uids: list[int]) -> str:
    return ", ".join(str(int(u)) for u in uids)


def global_user_agg(attach, *, start, end) -> str:
    inner = (
        f"SELECT s.uid AS uid, count(DISTINCT s.changeset_id) AS changesets, {_PSUM} "
        f"FROM changeset_stats s JOIN changesets c USING (changeset_id) "
        f"WHERE {_pg_global_window(start, end)} GROUP BY s.uid"
    )
    return _pg_query(attach, inner)


def global_editor_agg(attach, *, start, end) -> str:
    inner = (
        f"SELECT COALESCE(NULLIF(c.editor, ''), 'unknown') AS editor, s.uid AS uid, "
        f"count(DISTINCT s.changeset_id) AS cs, sum({_MC}) AS map_changes "
        f"FROM changeset_stats s JOIN changesets c USING (changeset_id) "
        f"WHERE {_pg_global_window(start, end)} GROUP BY 1, s.uid"
    )
    return _pg_query(attach, inner)


def global_tag_agg(attach, *, start, end) -> str:
    inner = (
        "SELECT (t).k AS k, (t).v AS v, sum((t).c) AS creates, sum((t).m) AS modifies, sum((t).l) AS length_m "
        f"FROM changeset_stats s JOIN changesets c USING (changeset_id), unnest(s.tags) AS t "
        f"WHERE {_pg_global_window(start, end)} GROUP BY (t).k, (t).v"
    )
    return _pg_query(attach, inner)


def global_user_tags(attach, uids: list[int], *, start, end) -> str:
    inner = (
        "SELECT s.uid AS uid, (t).k AS k, (t).v AS v, sum((t).c) AS c, sum((t).m) AS m, sum((t).l) AS l "
        f"FROM changeset_stats s JOIN changesets c USING (changeset_id), unnest(s.tags) AS t "
        f"WHERE s.uid IN ({_pg_uid_list(uids)}) AND {_pg_global_window(start, end)} GROUP BY s.uid, (t).k, (t).v"
    )
    return _pg_query(attach, inner)


def global_user_editors(attach, uids: list[int], *, start, end) -> str:
    inner = (
        "SELECT DISTINCT s.uid AS uid, c.editor AS editor "
        "FROM changeset_stats s JOIN changesets c USING (changeset_id) "
        f"WHERE s.uid IN ({_pg_uid_list(uids)}) AND {_pg_global_window(start, end)}"
    )
    return _pg_query(attach, inner)


def global_user_hashtags(attach, uids: list[int], *, start, end) -> str:
    inner = (
        "SELECT DISTINCT s.uid AS uid, lower(h) AS hashtag "
        f"FROM changeset_stats s JOIN changesets c USING (changeset_id), unnest(c.hashtags) AS h "
        f"WHERE s.uid IN ({_pg_uid_list(uids)}) AND {_pg_global_window(start, end)}"
    )
    return _pg_query(attach, inner)


def global_trending(attach, *, start, end) -> str:
    inner = (
        "SELECT lower(h) AS hashtag, count(DISTINCT c.changeset_id) AS changesets, count(DISTINCT c.uid) AS users "
        f"FROM changesets c, unnest(c.hashtags) AS h WHERE {_pg_global_window(start, end)} GROUP BY 1"
    )
    return _pg_query(attach, inner)


def _pg_user_window(frontier, start, end) -> str:
    bounds = [f"c.created_at >= {_pg_ts(frontier)}"]
    if start is not None:
        bounds.append(f"c.created_at >= {_pg_ts(start)}")
    if end is not None:
        bounds.append(f"c.created_at < {_pg_ts(end)}")
    return " AND ".join(bounds)


def recent_user_tags(attach, uids: list[int], *, prefixes, frontier, start=None, end=None) -> str:
    """Per (uid, key, value) recent tag breakdown for a fixed set of uids. Driven from the changeset_stats
    uid index (only those users' changesets), so it stays cheap regardless of the hashtag's total size."""
    uid_list = ", ".join(str(int(u)) for u in uids)
    ranges = " OR ".join(f"(lower(h) >= {_pg_str(lo)} AND lower(h) < {_pg_str(hi)})" for lo, hi in prefixes)
    inner = (
        f"SELECT s.uid AS uid, (t).k AS k, (t).v AS v, sum((t).c) AS c, sum((t).m) AS m, sum((t).l) AS l "
        f"FROM changeset_stats s JOIN changesets c USING (changeset_id), unnest(s.tags) AS t "
        f"WHERE s.uid IN ({uid_list}) AND {_pg_user_window(frontier, start, end)} "
        f"AND EXISTS (SELECT 1 FROM unnest(c.hashtags) AS h WHERE {ranges}) GROUP BY s.uid, (t).k, (t).v"
    )
    return _pg_query(attach, inner)


def recent_user_hashtags(attach, uids: list[int], *, prefixes, frontier, start=None, end=None) -> str:
    """Per (uid, hashtag) recent membership for a fixed set of uids, driven from the changeset_stats uid
    index; returns each matching hashtag the user contributed under in the window."""
    uid_list = ", ".join(str(int(u)) for u in uids)
    ranges = " OR ".join(f"(lower(h) >= {_pg_str(lo)} AND lower(h) < {_pg_str(hi)})" for lo, hi in prefixes)
    inner = (
        f"SELECT DISTINCT s.uid AS uid, lower(h) AS hashtag "
        f"FROM changeset_stats s JOIN changesets c USING (changeset_id), unnest(c.hashtags) AS h "
        f"WHERE s.uid IN ({uid_list}) AND {_pg_user_window(frontier, start, end)} AND ({ranges})"
    )
    return _pg_query(attach, inner)


def recent_user_cooccur_hashtags(attach, uids: list[int], *, prefixes, frontier, start=None, end=None) -> str:
    """Per (uid, hashtag) recent membership over ALL hashtags carried by each user's matched changesets,
    so the profile can show which other hashtags and projects the user tagged alongside the search."""
    uid_list = ", ".join(str(int(u)) for u in uids)
    ranges = " OR ".join(f"(lower(x) >= {_pg_str(lo)} AND lower(x) < {_pg_str(hi)})" for lo, hi in prefixes)
    inner = (
        f"SELECT DISTINCT s.uid AS uid, lower(h) AS hashtag "
        f"FROM changeset_stats s JOIN changesets c USING (changeset_id), unnest(c.hashtags) AS h "
        f"WHERE s.uid IN ({uid_list}) AND {_pg_user_window(frontier, start, end)} "
        f"AND EXISTS (SELECT 1 FROM unnest(c.hashtags) AS x WHERE {ranges})"
    )
    return _pg_query(attach, inner)


def recent_user_editors(attach, uids: list[int], *, prefixes, frontier, start=None, end=None) -> str:
    """Per (uid, editor) recent membership: the distinct editors each user used on their matched changesets."""
    uid_list = ", ".join(str(int(u)) for u in uids)
    ranges = " OR ".join(f"(lower(x) >= {_pg_str(lo)} AND lower(x) < {_pg_str(hi)})" for lo, hi in prefixes)
    inner = (
        f"SELECT DISTINCT s.uid AS uid, c.editor AS editor "
        f"FROM changeset_stats s JOIN changesets c USING (changeset_id) "
        f"WHERE s.uid IN ({uid_list}) AND {_pg_user_window(frontier, start, end)} "
        f"AND EXISTS (SELECT 1 FROM unnest(c.hashtags) AS x WHERE {ranges})"
    )
    return _pg_query(attach, inner)


def _window_clause(start: dt.datetime | None, end: dt.datetime | None) -> tuple[str, list[object]]:
    """Optional `created_at` bounds as SQL fragments plus their params, in order. Half-open [start, end):
    inclusive lower, exclusive upper, matching the frontier split and the v1 API's window semantics."""
    parts: list[str] = []
    params: list[object] = []
    if start is not None:
        parts.append(" AND created_at >= ?")
        params.append(start)
    if end is not None:
        parts.append(" AND created_at < ?")
        params.append(end)
    return "".join(parts), params


def _recent_from_base(stats_rel: str, changesets_rel: str, window_sql: str, prefix_pred: str) -> str:
    """Per-changeset rows built on the fly from the base tables for matching changesets on/after the
    frontier, counts summed across seq rows and native `tags` merged to match the rollup shape. `prefix_pred`
    OR-s `lower(h) >= ? AND lower(h) < ?`; placeholders in order: frontier, (window bounds), (prefix bounds)."""
    sums = ", ".join(f"SUM(s.{c}) AS {c}" for c in COUNT_COLS)
    payload = ", ".join(f"p.{c}" for c in COUNT_COLS)
    return f"""
        WITH matched AS (
            SELECT changeset_id, uid, editor, created_at
            FROM {changesets_rel}
            WHERE created_at >= ?{window_sql} AND len(list_filter(hashtags, h -> {prefix_pred})) > 0
        ),
        per_cs AS (
            SELECT s.changeset_id, {sums}
            FROM {stats_rel} s JOIN matched USING (changeset_id) GROUP BY s.changeset_id
        ),
        tag_rows AS (
            SELECT changeset_id, t.k AS k, t.v AS v,
                   SUM(t.c) AS c, SUM(t.m) AS m, SUM(t.l) AS l
            FROM (
                SELECT s.changeset_id, UNNEST(s.tags) AS t
                FROM {stats_rel} s JOIN matched USING (changeset_id)
                WHERE s.tags IS NOT NULL AND len(s.tags) > 0
            )
            GROUP BY changeset_id, t.k, t.v
        ),
        tags AS (
            SELECT changeset_id, list(struct_pack(k := k, v := v, c := c, m := m, l := l)) AS tags
            FROM tag_rows GROUP BY changeset_id
        )
        SELECT m.changeset_id, m.uid, m.editor, m.created_at, {payload}, COALESCE(t.tags, []) AS tags
        FROM matched m JOIN per_cs p USING (changeset_id) LEFT JOIN tags t USING (changeset_id)
    """


def hashtag_scope(
    history_rel: str,
    recent_stats_rel: str,
    recent_changesets_rel: str,
    *,
    prefixes: list[tuple[str, str]],
    frontier: dt.datetime,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> tuple[str, list[object]]:
    """One deduped per-changeset relation for hashtag prefix ranges [lo, hi): the rollup for history
    (< frontier) unioned with the recent tail (>= frontier), deduped by changeset_id, optional [start,
    end) window. Returns (sql, params)."""
    if not prefixes:
        raise ValueError("prefixes must be non-empty")
    window_sql, window_params = _window_clause(start, end)
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
    recent_pred = " OR ".join("(lower(h) >= ? AND lower(h) < ?)" for _ in prefixes)
    recent = _recent_from_base(recent_stats_rel, recent_changesets_rel, window_sql, recent_pred)
    sql = f"""
        SELECT DISTINCT ON (changeset_id) {_PROJECTION} FROM (
            SELECT {_PROJECTION} FROM {history_rel}
                WHERE ({hist_pred}) AND created_at < ?{window_sql}
            UNION ALL
            ({recent})
        )
    """
    params: list[object] = [
        *prefix_params,
        frontier,
        *window_params,
        frontier,
        *window_params,
        *prefix_params,
    ]
    return sql, params


def history_dedup_scope(
    history_rel: str,
    *,
    prefixes: list[tuple[str, str]],
    frontier: dt.datetime,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> tuple[str, list[object]]:
    """The deduped per-changeset HISTORY relation alone (< frontier), as its own SELECT so a caller can
    aggregate it and the recent side separately, avoiding a DISTINCT ON over the full union that would
    materialize the whole 14M-row intermediate for a big hashtag. `prefixes` non-empty. Returns (sql, params)."""
    if not prefixes:
        raise ValueError("prefixes must be non-empty")
    window_sql, window_params = _window_clause(start, end)
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
    sql = (
        f"SELECT DISTINCT ON (changeset_id) {_PROJECTION} FROM {history_rel} "
        f"WHERE ({hist_pred}) AND created_at < ?{window_sql}"
    )
    return sql, [*prefix_params, frontier, *window_params]


def history_tag_agg(
    history_rel: str,
    *,
    prefixes: list[tuple[str, str]],
    frontier: dt.datetime,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> tuple[str, list[object]]:
    """Per (key, value) history tag breakdown, unnest-first so the per-hashtag duplication collapses in a
    narrow (changeset_id, k, v) aggregate that spills, not a DISTINCT ON holding every tag list."""
    if not prefixes:
        raise ValueError("prefixes must be non-empty")
    window_sql, window_params = _window_clause(start, end)
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
    sql = (
        "SELECT k, v, SUM(c) AS creates, SUM(m) AS modifies, SUM(l) AS length_m FROM ("
        "SELECT changeset_id, t.k AS k, t.v AS v, ANY_VALUE(t.c) AS c, ANY_VALUE(t.m) AS m, ANY_VALUE(t.l) AS l "
        f"FROM (SELECT changeset_id, UNNEST(tags) AS t FROM {history_rel} "
        f"WHERE ({hist_pred}) AND created_at < ?{window_sql}) "
        "GROUP BY changeset_id, k, v) GROUP BY k, v"
    )
    return sql, [*prefix_params, frontier, *window_params]


def history_scope_count(
    history_rel: str,
    *,
    prefixes: list[tuple[str, str]],
    frontier: dt.datetime,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> tuple[str, list[object]]:
    """A CHEAP raw `count(*)` of history rows matching the hashtag scope + window (no dedup, hashtag range
    prunes row groups: ~sub-second even for a 14M-changeset hashtag). Used to decide whether a per-user
    tag breakdown is affordable before paying for it. Returns (sql, params)."""
    if not prefixes:
        raise ValueError("prefixes must be non-empty")
    window_sql, window_params = _window_clause(start, end)
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
    sql = f"SELECT count(*) FROM {history_rel} WHERE ({hist_pred}) AND created_at < ?{window_sql}"
    return sql, [*prefix_params, frontier, *window_params]


def recent_scope(
    recent_stats_rel: str,
    recent_changesets_rel: str,
    *,
    prefixes: list[tuple[str, str]],
    frontier: dt.datetime,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> tuple[str, list[object]]:
    """The recent per-changeset relation alone (created_at >= frontier), already one row per changeset.
    Companion to `history_dedup_scope` for the aggregate-then-combine path. Returns (sql, params)."""
    if not prefixes:
        raise ValueError("prefixes must be non-empty")
    window_sql, window_params = _window_clause(start, end)
    prefix_params = [bound for pair in prefixes for bound in pair]
    recent_pred = " OR ".join("(lower(h) >= ? AND lower(h) < ?)" for _ in prefixes)
    sql = "(" + _recent_from_base(recent_stats_rel, recent_changesets_rel, window_sql, recent_pred) + ")"
    return sql, [frontier, *window_params, *prefix_params]


def map_scope(
    history_rollup_rel: str,
    recent_changesets_rel: str,
    *,
    prefixes: list[tuple[str, str]],
    frontier: dt.datetime,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> tuple[str, list[object]]:
    """Deduped changeset centroids `(changeset_id, uid, lon, lat)` for the hashtag union: from the rollup
    `lon`/`lat` (history) and the base changesets bbox midpoint (recent). No-bbox changesets are dropped."""
    if not prefixes:
        raise ValueError("prefixes must be non-empty")
    window_sql, window_params = _window_clause(start, end)
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
    recent_pred = " OR ".join("(lower(h) >= ? AND lower(h) < ?)" for _ in prefixes)
    sql = f"""
        SELECT DISTINCT ON (changeset_id) changeset_id, uid, lon, lat FROM (
            SELECT changeset_id, uid, lon, lat FROM {history_rollup_rel}
                WHERE ({hist_pred}) AND created_at < ?{window_sql} AND lon IS NOT NULL
            UNION ALL
            SELECT changeset_id, uid, (min_lon + max_lon) / 2.0 AS lon, (min_lat + max_lat) / 2.0 AS lat
                FROM {recent_changesets_rel}
                WHERE created_at >= ?{window_sql} AND min_lon IS NOT NULL
                  AND len(list_filter(hashtags, h -> {recent_pred})) > 0
        )
    """
    params: list[object] = [*prefix_params, frontier, *window_params, frontier, *window_params, *prefix_params]
    return sql, params
