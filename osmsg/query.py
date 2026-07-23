"""The hashtag query surface: summary, leaderboard, tag breakdown, editors. Built once here from the
catalog + stats vocabulary so the CLI and API compute identically. The caller supplies a DuckDB
connection with the sources reachable (local tables, an attached Postgres, or read_parquet('hf://...')).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import duckdb

from .catalog import hashtag_scope, map_scope
from .stats import map_changes_sum, prefix_upper_bound, sum_cols, tag_breakdown_from_list


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


def _scope(
    hashtag: str | list[str], s: Sources, start: dt.datetime | None = None, end: dt.datetime | None = None
) -> tuple[str, list[object]]:
    return hashtag_scope(
        s.history_rel,
        s.recent_stats_rel,
        s.recent_changesets_rel,
        prefixes=_prefixes(hashtag),
        frontier=s.frontier,
        start=start,
        end=end,
    )


def summary(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> dict[str, Any]:
    """Totals for the hashtag: distinct users, changesets, and the full element breakdown. Optional
    half-open [start, end) window."""
    scope, params = _scope(hashtag, s, start, end)
    res = con.execute(
        f"WITH m AS ({scope}) SELECT count(DISTINCT uid) AS users, count(*) AS changesets, "
        f"{sum_cols()}, {map_changes_sum()} FROM m",
        params,
    )
    return _rows(res)[0]


def leaderboard(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    limit: int = 100,
    offset: int = 0,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Top users for the hashtag by map_changes, with their full breakdown and editors used. Optional
    half-open [start, end) window."""
    scope, params = _scope(hashtag, s, start, end)
    res = con.execute(
        f"""
        WITH m AS ({scope})
        SELECT m.uid, u.username AS name, count(*) AS changesets, {sum_cols()}, {map_changes_sum()},
               list(DISTINCT m.editor) AS editors,
               ROW_NUMBER() OVER (ORDER BY {map_changes_sum().split(" AS ")[0]} DESC, m.uid ASC) AS rank
        FROM m LEFT JOIN {s.users_rel} u USING (uid)
        GROUP BY m.uid, u.username
        ORDER BY map_changes DESC, m.uid ASC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    return _rows(res)


def tags(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    limit: int = 100,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Tag key/value breakdown (creates/modifies) over the hashtag's deduped changesets. Optional
    half-open [start, end) window."""
    scope, params = _scope(hashtag, s, start, end)
    res = con.execute(
        f"WITH m AS ({scope}) {tag_breakdown_from_list('m')} ORDER BY creates DESC LIMIT ?",
        [*params, limit],
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
    """Editor breakdown for the hashtag: changesets and distinct users per editor. Optional half-open
    [start, end) window."""
    scope, params = _scope(hashtag, s, start, end)
    res = con.execute(
        f"WITH m AS ({scope}) "
        "SELECT COALESCE(NULLIF(editor, ''), 'unknown') AS editor, count(*) AS changesets, "
        "count(DISTINCT uid) AS users FROM m GROUP BY 1 ORDER BY changesets DESC",
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
    """Per-bucket activity for the hashtag: changesets, distinct users, and map_changes per day, week,
    or month (UTC). Optional half-open [start, end) window."""
    if interval not in TREND_INTERVALS:
        raise ValueError(f"interval must be one of {TREND_INTERVALS}")
    scope, params = _scope(hashtag, s, start, end)
    res = con.execute(
        f"WITH m AS ({scope}) "
        f"SELECT CAST(date_trunc('{interval}', created_at) AS DATE)::VARCHAR AS bucket, "
        f"count(*) AS changesets, count(DISTINCT uid) AS users, {map_changes_sum()} "
        "FROM m GROUP BY 1 ORDER BY 1",
        params,
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
    map. Optional half-open [start, end) window. History centroids come from the rollup, recent from the
    base changesets bbox; the rollup must carry `lon`/`lat` (published rollups built after the map change
    do)."""
    sql, params = map_scope(
        s.history_rel, s.recent_changesets_rel, prefixes=_prefixes(hashtag), frontier=s.frontier, start=start, end=end
    )
    res = con.execute(f"{sql} LIMIT ?", [*params, limit])
    return _rows(res)
