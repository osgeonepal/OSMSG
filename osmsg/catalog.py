"""Assemble query sources into one relation so `stats.py` runs a single query regardless of where
data lives. History (older than the published frontier) comes from the `hashtag_changeset` rollup;
the recent tail (from the frontier on) is derived on the fly from the base tables, filtered by the
hashtag first so only matching changesets are read. The two are unioned and split at the frontier, so
nothing is counted twice and nothing is dropped, and the recent side is always as fresh as the base.

The base is just relations: a local DuckDB store's `changeset_stats`/`changesets`, or an attached
Postgres's `pg.changeset_stats`/`pg.changesets`. Either way the query runs in DuckDB.
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
    """Per-changeset rows built on the fly from the base tables for changesets whose hashtags match any
    requested prefix and are on/after the frontier. Counts are summed across a changeset's seq rows and
    the native `tags` lists merged, matching the rollup shape exactly (one tag representation, one
    breakdown path). The changeset filter runs first, so only matching changesets are read. `prefix_pred`
    is the OR of `lower(h) >= ? AND lower(h) < ?` range tests on the lambda var. Placeholders, in order:
    frontier, (window bounds), (prefix bounds)."""
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
    """One deduped per-changeset relation for one or more hashtag prefix ranges [lo, hi): the rollup for
    history (created_at < frontier, hashtag range prunes row groups) unioned with the recent tail
    derived from the base (created_at >= frontier). A changeset matching more than one prefix (or
    carrying two matching hashtags) is deduped by changeset_id, so it counts once. An optional half-open
    [start, end) window bounds created_at on both sides, so it intersects the frontier split cleanly (a
    window entirely before the frontier reads only history; entirely after, only the base). `prefixes`
    must be non-empty. Returns (sql, params)."""
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


def map_scope(
    history_rollup_rel: str,
    recent_changesets_rel: str,
    *,
    prefixes: list[tuple[str, str]],
    frontier: dt.datetime,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> tuple[str, list[object]]:
    """Deduped changeset centroids `(changeset_id, uid, lon, lat)` for the hashtag union, for the map.
    History centroids come from the rollup's `lon`/`lat`; recent centroids from the base changesets bbox
    midpoint (`recent_changesets_rel` must expose min_lon/min_lat/max_lon/max_lat, e.g. the published
    changesets dataset or the Postgres `changesets` table). Changesets with no bbox are dropped. Same
    frontier split and optional [start, end) window as `hashtag_scope`. `prefixes` must be non-empty."""
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
