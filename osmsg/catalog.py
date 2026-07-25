"""Assemble query sources into one relation so `stats.py` runs a single query wherever data lives: history
(< frontier) from the `hashtag_changeset` rollup, the recent tail from the base tables (a local DuckDB store
or attached Postgres, hashtag-filtered first), unioned and split at the frontier. The query runs in DuckDB.
"""

from __future__ import annotations

import datetime as dt

from .stats import COUNT_COLS

_PROJECTION = f"changeset_id, uid, editor, created_at, {', '.join(COUNT_COLS)}, tags"


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
                   SUM(t.c) AS c, SUM(t.m) AS m, SUM(t.len_m) AS len_m
            FROM (
                SELECT s.changeset_id, UNNEST(s.tags) AS t
                FROM {stats_rel} s JOIN matched USING (changeset_id)
                WHERE s.tags IS NOT NULL AND len(s.tags) > 0
            )
            GROUP BY changeset_id, t.k, t.v
        ),
        tags AS (
            SELECT changeset_id, list(struct_pack(k := k, v := v, c := c, m := m, len_m := len_m)) AS tags
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
    (< frontier) unioned with the recent tail from the base (>= frontier), deduped by changeset_id so a
    changeset matching several prefixes counts once. Optional [start, end) window bounds created_at.
    Returns (sql, params)."""
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
