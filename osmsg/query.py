"""The hashtag query surface (summary, leaderboard, tags, editors, trends, map), built from the catalog
+ stats vocabulary so the CLI and API compute identically. History and recent are disjoint at the
frontier, so each function aggregates them separately and combines, keeping large hashtags fast."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import os
import pathlib
import time
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
    # When set, all-time history aggregates are memoized to parquet files here (keyed by prefix+frontier),
    # so a mega-hashtag pays the dedup once. History is immutable below the frontier, so a hit is exact.
    cache_dir: str | None = None
    # When set, the recent count endpoints read this counts-only per-changeset parquet (a TTL-refreshed
    # slice of the matched tail) instead of re-scanning Postgres each request. Tag/per-user paths still
    # use `pg_attach` (the slice carries no tags), so this coexists with an attached Postgres.
    recent_tail_rel: str | None = None


def _rows(result) -> list[dict[str, Any]]:
    cols = [d[0] for d in result.description]
    return [dict(zip(cols, r, strict=True)) for r in result.fetchall()]


def _like_escape(value: str) -> str:
    """Escape LIKE metacharacters so a contributor search matches them literally. Without this `_`
    matches any character and `%` any sequence, so searching `a_il` returns `Anil` and `%` returns
    everyone. Paired with `ESCAPE '\\'` at the call site."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Exact-match upper bound. Hashtags are #[\w-]+, so the smallest continuation byte on any longer hashtag is
# '-' (0x2D); a bound below it makes [lo, lo + _EXACT_SENTINEL) match lo alone, excluding #hotosm-project-11
# when searching #hotosm-project-1. A space works on both DuckDB and Postgres (COLLATE "C"), unlike a null byte.
_EXACT_SENTINEL = " "


def _prefixes(hashtag: str | list[str], *, exact: bool = False) -> list[tuple[str, str]]:
    """Normalize one hashtag or many into deduped `(lo, hi)` range pairs, case-insensitive and
    order-preserving. Each hashtag matches as a prefix, or only itself when `exact`; the scope is the
    union across them."""
    tags = [hashtag] if isinstance(hashtag, str) else list(hashtag)
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for h in tags:
        lo = "#" + h.strip().lower().lstrip("#")
        if lo == "#" or lo in seen:
            continue
        seen.add(lo)
        pairs.append((lo, lo + _EXACT_SENTINEL if exact else prefix_upper_bound(lo)))
    if not pairs:
        raise ValueError("at least one non-empty hashtag is required")
    return pairs


def _history(s: Sources, prefixes, start, end) -> tuple[str, list[object]]:
    """The deduped history-side per-changeset SELECT (sql, params) for the hashtag scope + window."""
    return catalog.history_dedup_scope(s.history_rel, prefixes=prefixes, frontier=s.frontier, start=start, end=end)


def _cache_path(s: Sources, prefixes, grain: str, start, end) -> pathlib.Path | None:
    """The memo file for an all-time history aggregate of this hashtag scope, or None when caching does
    not apply: no cache dir, or a windowed query (only all-time, the slow case, is cached). The frontier
    is in the key, so a newly published month yields a new path and the stale one is simply unused."""
    if s.cache_dir is None or start is not None or end is not None:
        return None
    seed = repr((sorted(prefixes), grain, s.frontier.astimezone(dt.UTC).isoformat()))
    digest = hashlib.sha1(seed.encode()).hexdigest()[:16]
    return pathlib.Path(s.cache_dir) / f"{grain}-{digest}.parquet"


def _materialize_history(con, temp: str, select_sql: str, params, path: pathlib.Path | None) -> None:
    """CREATE OR REPLACE TEMP TABLE `temp` from `select_sql`, memoized to `path` when set. A hit reads the
    parquet; a miss runs the aggregate then writes it via a temp file + atomic rename (concurrent misses
    produce identical content, so last-writer-wins is safe). The cached rows equal the live aggregate, so
    a hit and a miss return the same numbers."""
    if path is not None and path.exists():
        con.execute(f"CREATE OR REPLACE TEMP TABLE {temp} AS SELECT * FROM read_parquet('{path.as_posix()}')")
        return
    con.execute(f"CREATE OR REPLACE TEMP TABLE {temp} AS {select_sql}", params)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        con.execute(f"COPY {temp} TO '{tmp.as_posix()}' (FORMAT parquet)")
        os.replace(tmp, path)


# All-time per-user leaderboard tags are gated off the request path for a mega hashtag and filled by a
# background warm instead; warm at most this many top contributors so the cache stays small.
_WARM_TAG_USERS = 500


def _warm_leaderboard_tags(con, prefixes: list[tuple[str, str]], s: Sources) -> None:
    """Compute + cache the per-user history tag breakdown for the top contributors of an all-time mega
    hashtag, so the leaderboard can serve complete per-user tags (this cache + live recent) on return."""
    path = _cache_path(s, prefixes, "lb_tags", None, None)
    if path is None or path.exists():
        return
    # Only mega hashtags gate their per-user tags off the request path; smaller ones serve them inline, so
    # their cache would never be read.
    count_sql, count_params = catalog.history_scope_count(
        s.history_rel, prefixes=prefixes, frontier=s.frontier, start=None, end=None
    )
    count_row = con.execute(count_sql, count_params).fetchone()
    if not count_row or count_row[0] <= _MAX_TAG_ROWS:
        return
    hsql, hp = _history(s, prefixes, None, None)
    top = con.execute(
        f"SELECT uid FROM ({hsql}) GROUP BY uid ORDER BY SUM({map_changes_expr()}) DESC LIMIT {_WARM_TAG_USERS}", hp
    ).fetchall()
    if not top:
        return
    uid_list = ", ".join(str(int(u)) for (u,) in top)
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
    # Unnest-first with a per-changeset dedup so the wide tags struct never flows through a multi-million-row
    # DISTINCT ON; the uid filter is applied in the scan so only the top contributors' rows are read.
    sql = (
        "SELECT uid, k, v, SUM(c) AS c, SUM(m) AS m, SUM(l) AS l FROM ("
        "SELECT uid, changeset_id, t.k AS k, t.v AS v, ANY_VALUE(t.c) AS c, ANY_VALUE(t.m) AS m, "
        "ANY_VALUE(t.l) AS l "
        f"FROM (SELECT uid, changeset_id, UNNEST(tags) AS t FROM {s.history_rel} "
        f"WHERE ({hist_pred}) AND created_at < ? AND uid IN ({uid_list})) "
        "GROUP BY uid, changeset_id, k, v) GROUP BY uid, k, v"
    )
    _materialize_history(con, "_warm_lb_tags", sql, [*prefix_params, s.frontier], path)


def _warm_trending_cooccur(con, prefixes: list[tuple[str, str]], s: Sources) -> None:
    """Compute + cache the all-time history co-occurring `(hashtag, uid, map_changes)` for a hashtag, so the
    trending endpoint can serve complete all-time results (this cache + live recent) on return."""
    path = _cache_path(s, prefixes, "cooccur", None, None)
    if path is None or path.exists():
        return
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
    matched_cs = f"SELECT DISTINCT changeset_id FROM {s.history_rel} WHERE ({hist_pred}) AND created_at < ?"
    # Pre-aggregate to one row per (hashtag, uid) so the read stays small and fast; the recent side is also
    # per (hashtag, uid), so the union re-dedups exactly (a contributor in both sides counts once).
    sql = (
        f"SELECT h.hashtag AS hashtag, h.uid AS uid, SUM({map_changes_expr('h')}) AS mc "
        f"FROM {s.history_rel} h JOIN ({matched_cs}) m USING (changeset_id) WHERE h.created_at < ? "
        "GROUP BY h.hashtag, h.uid"
    )
    _materialize_history(con, "_warm_cooccur", sql, [*prefix_params, s.frontier, s.frontier], path)


def warm_all_time(con, hashtag: str | list[str], s: Sources, *, exact: bool = False) -> None:
    """Fill the all-time history caches (leaderboard per-user tags, trending co-occurring) for a hashtag off
    the request path. Idempotent: each cache is written once per frontier and skipped once present."""
    prefixes = _prefixes(hashtag, exact=exact)
    # A warm writes unordered cache files, so drop order preservation to keep these large aggregates spilling
    # to disk under the memory cap instead of holding the whole result in memory.
    con.execute("SET preserve_insertion_order=false")
    _warm_leaderboard_tags(con, prefixes, s)
    _warm_trending_cooccur(con, prefixes, s)


def all_time_warm_pending(s: Sources, hashtag: str | list[str], *, exact: bool = False) -> bool:
    """True when the all-time warm is worth firing, keyed on the co-occurring cache that every warm writes
    last. False when caching is off or the warm has already run."""
    path = _cache_path(s, _prefixes(hashtag, exact=exact), "cooccur", None, None)
    return path is not None and not path.exists()


# The recent tail advances only as the worker ingests, so its aggregate is memoized for this long before a
# re-scan. Small enough that a public leaderboard is effectively live; large enough to elide repeat scans.
RECENT_TTL_SECONDS = int(os.getenv("OSMSG_RECENT_TTL_SECONDS", "60"))


def _refresh_slice(con, path: pathlib.Path, rel: str) -> None:
    """Write `rel` (a Postgres-side relation) to `path` as parquet unless a fresh copy exists (mtime within
    the TTL). Atomic rename so a concurrent refresh cannot expose a half-written file."""
    if path.exists() and (time.time() - path.stat().st_mtime) < RECENT_TTL_SECONDS:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    con.execute(f"COPY (SELECT * FROM {rel}) TO '{tmp.as_posix()}' (FORMAT parquet)")
    os.replace(tmp, path)


def _with_recent_tail_cache(con, s: Sources, prefixes, start, end) -> Sources:
    """For an all-time query over attached Postgres with a cache dir, materialize the matched recent tail
    (one counts row per changeset, created_at >= frontier, aggregated Postgres-side via the changeset_hashtag
    index) into a TTL-refreshed local parquet, and return a Sources whose count endpoints read it. The slice
    is counts-only, so tag and per-user paths keep using Postgres (`pg_attach` stays set). Windowed or
    non-Postgres queries are returned unchanged so the recent side stays live."""
    if not (s.pg_attach and s.cache_dir) or start is not None or end is not None:
        return s
    seed = repr((sorted(prefixes), s.frontier.astimezone(dt.UTC).isoformat()))
    digest = hashlib.sha1(seed.encode()).hexdigest()[:16]
    path = pathlib.Path(s.cache_dir) / f"recent_tail-{digest}.parquet"
    matched = catalog._pg_matched(prefixes, s.frontier, None, None)
    psum = ", ".join(f"sum(s.{c}) AS {c}" for c in COUNT_COLS)
    pc_cols = ", ".join(f"pc.{c}" for c in COUNT_COLS)
    inner = (
        f"WITH m AS ({matched}), "
        f"pc AS (SELECT s.changeset_id, max(s.uid) AS uid, {psum} "
        f"FROM m JOIN changeset_stats s USING (changeset_id) GROUP BY s.changeset_id) "
        f"SELECT pc.changeset_id, pc.uid, c.editor, c.created_at, c.hashtags, {pc_cols} "
        f"FROM pc JOIN changesets c USING (changeset_id)"
    )
    _refresh_slice(con, path, catalog._pg_query(s.pg_attach, inner))
    tail = f"read_parquet('{path.as_posix()}')"
    return dataclasses.replace(s, recent_tail_rel=tail, recent_stats_rel=tail, recent_changesets_rel=tail)


def _use_pg(s: Sources) -> bool:
    """The recent count side aggregates in Postgres only when attached AND no local tail slice is cached.
    With a slice, the count endpoints read it; tag/per-user paths still branch on `pg_attach` directly."""
    return s.pg_attach is not None and s.recent_tail_rel is None


def _recent_perchangeset(s: Sources, prefixes, start, end) -> tuple[str, list[object]]:
    """The recent-side per-changeset relation: the cached counts-only tail slice when present, else built
    live from the local store's base tables (columnar, small tail)."""
    if s.recent_tail_rel is not None:
        return f"(SELECT * FROM {s.recent_tail_rel})", []
    return catalog.recent_scope(
        s.recent_stats_rel, s.recent_changesets_rel, prefixes=prefixes, frontier=s.frontier, start=start, end=end
    )


def _recent_users(s: Sources, prefixes, start, end) -> tuple[str, list[object]]:
    """Recent side as a per-uid aggregate relation (uid, changesets, count cols): aggregated in Postgres
    on the API path, or rolled up from the store's per-changeset scan otherwise."""
    if _use_pg(s):
        return catalog.recent_user_agg(s.pg_attach, prefixes=prefixes, frontier=s.frontier, start=start, end=end), []
    rsql, rp = _recent_perchangeset(s, prefixes, start, end)
    return f"(SELECT uid, count(*) AS changesets, {_SUM_AS} FROM ({rsql}) GROUP BY uid)", rp


_SUM_AS = ", ".join(f"SUM({c}) AS {c}" for c in COUNT_COLS)
_COLS = ", ".join(COUNT_COLS)


def summary(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    exact: bool = False,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> dict[str, Any]:
    """Totals for the hashtag: distinct users, changesets, the full element breakdown. Optional [start,
    end) window. Aggregates each side per user, then combines (disjoint by frontier -> additive counts;
    distinct users is the count of combined per-user rows)."""
    prefixes = _prefixes(hashtag, exact=exact)
    s = _with_recent_tail_cache(con, s, prefixes, start, end)
    hsql, hp = _history(s, prefixes, start, end)
    rrel, rp = _recent_users(s, prefixes, start, end)
    _materialize_history(
        con,
        "_q_hu",
        f"SELECT uid, count(*) AS changesets, {_SUM_AS} FROM ({hsql}) GROUP BY uid",
        hp,
        _cache_path(s, prefixes, "summary_users", start, end),
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

# The co-occurring self-join scans every rollup row in the window (all hashtags), which only the window's
# created_at partitions prune. Run it only when that scan is bounded; an all-time or very wide window reads
# the whole rollup (~20s+), so skip the history side there and use the live recent tail alone.
_COOCCUR_MAX_SCAN_ROWS = 3_000_000


def _cooccur_history_cheap(
    con: duckdb.DuckDBPyConnection, s: Sources, start: dt.datetime | None, end: dt.datetime | None
) -> bool:
    window_sql, window_params = catalog._window_clause(start, end)
    row = con.execute(
        f"SELECT count(*) FROM {s.history_rel} WHERE created_at < ?{window_sql}",
        [s.frontier, *window_params],
    ).fetchone()
    return bool(row) and 0 < row[0] <= _COOCCUR_MAX_SCAN_ROWS


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
        f"SELECT uid, t.k AS k, t.v AS v, t.c AS c, t.m AS m, t.l AS l "
        f"FROM (SELECT uid, UNNEST(tags) AS t FROM {hist_src} WHERE uid IN ({ph}))"
    )
    if s.pg_attach:
        rrel = catalog.recent_user_tags(s.pg_attach, uids, prefixes=prefixes, frontier=s.frontier, start=start, end=end)
        recent = f"SELECT uid, k, v, c, m, l FROM {rrel}"
        params = [*hp, *uids]
    else:
        rsql, rp = _recent_perchangeset(s, prefixes, start, end)
        recent = (
            f"SELECT uid, t.k AS k, t.v AS v, t.c AS c, t.m AS m, t.l AS l "
            f"FROM (SELECT uid, UNNEST(tags) AS t FROM ({rsql}) WHERE uid IN ({ph}))"
        )
        params = [*hp, *uids, *rp, *uids]
    tag_rows = con.execute(
        f"SELECT uid, k, v, SUM(c) AS c, SUM(m) AS m, SUM(l) AS l FROM ({hist} UNION ALL {recent}) GROUP BY uid, k, v",
        params,
    ).fetchall()
    per_uid: dict[Any, list[dict[str, Any]]] = {}
    for uid, k, v, c, m, length_m in tag_rows:
        per_uid.setdefault(uid, []).append({"k": k, "v": v, "c": c, "m": m, "l": length_m})
    for r in rows:
        if per_uid.get(r["uid"]):
            r["tag_stats"] = _tags_to_nested(per_uid[r["uid"]])


def _attach_user_tags_cached(
    con: duckdb.DuckDBPyConnection,
    rows: list[dict[str, Any]],
    s: Sources,
    prefixes: list[tuple[str, str]],
    start: dt.datetime | None,
    end: dt.datetime | None,
) -> bool:
    """Attach per-user `tag_stats` from the warmed history-tags cache plus the live recent tail. Returns True
    when the cache was present and used, False to signal the caller to gate."""
    for r in rows:
        r["tag_stats"] = {}
    path = _cache_path(s, prefixes, "lb_tags", start, end)
    if path is None or not path.exists() or not rows:
        return False
    uids = [r["uid"] for r in rows]
    ph = ", ".join("?" for _ in uids)
    hist = f"SELECT uid, k, v, c, m, l FROM read_parquet('{path.as_posix()}') WHERE uid IN ({ph})"
    if s.pg_attach:
        rrel = catalog.recent_user_tags(s.pg_attach, uids, prefixes=prefixes, frontier=s.frontier, start=start, end=end)
        recent, params = f"SELECT uid, k, v, c, m, l FROM {rrel}", [*uids]
    else:
        rsql, rp = _recent_perchangeset(s, prefixes, start, end)
        recent = (
            f"SELECT uid, t.k AS k, t.v AS v, t.c AS c, t.m AS m, t.l AS l "
            f"FROM (SELECT uid, UNNEST(tags) AS t FROM ({rsql}) WHERE uid IN ({ph}))"
        )
        params = [*uids, *rp, *uids]
    tag_rows = con.execute(
        f"SELECT uid, k, v, SUM(c) AS c, SUM(m) AS m, SUM(l) AS l FROM ({hist} UNION ALL {recent}) GROUP BY uid, k, v",
        params,
    ).fetchall()
    per_uid: dict[Any, list[dict[str, Any]]] = {}
    for uid, k, v, c, m, length_m in tag_rows:
        per_uid.setdefault(uid, []).append({"k": k, "v": v, "c": c, "m": m, "l": length_m})
    for r in rows:
        if per_uid.get(r["uid"]):
            r["tag_stats"] = _tags_to_nested(per_uid[r["uid"]])
    return True


def _attach_user_hashtags(
    con: duckdb.DuckDBPyConnection,
    rows: list[dict[str, Any]],
    s: Sources,
    prefixes: list[tuple[str, str]],
    start: dt.datetime | None,
    end: dt.datetime | None,
) -> None:
    """In-place: attach each returned user's RECENT co-occurring hashtags from the live tail only (shown as
    "Recent hashtags" in the UI); the all-time aggregate is served by the /hashtags endpoint."""
    for r in rows:
        r["hashtags"] = []
    if not rows:
        return
    uids = [r["uid"] for r in rows]
    if s.pg_attach:
        rrel = catalog.recent_user_cooccur_hashtags(
            s.pg_attach, uids, prefixes=prefixes, frontier=s.frontier, start=start, end=end
        )
        recent, params = f"SELECT uid, hashtag FROM {rrel}", []
    else:
        ph = ", ".join("?" for _ in uids)
        window_sql, window_params = catalog._window_clause(start, end)
        prefix_params = [bound for pair in prefixes for bound in pair]
        recent_pred = " OR ".join("(lower(x) >= ? AND lower(x) < ?)" for _ in prefixes)
        recent = (
            f"SELECT uid, lower(h) AS hashtag FROM (SELECT uid, UNNEST(hashtags) AS h "
            f"FROM {s.recent_changesets_rel} "
            f"WHERE created_at >= ?{window_sql} AND uid IN ({ph}) "
            f"AND EXISTS (SELECT 1 FROM UNNEST(hashtags) AS t(x) WHERE {recent_pred}))"
        )
        params = [s.frontier, *window_params, *uids, *prefix_params]
    result = con.execute(
        f"SELECT uid, list(DISTINCT hashtag) AS hashtags FROM ({recent}) GROUP BY uid",
        params,
    ).fetchall()
    by_uid = {uid: list(hashtags or []) for uid, hashtags in result}
    for r in rows:
        r["hashtags"] = by_uid.get(r["uid"], [])


def _attach_user_editors(
    con: duckdb.DuckDBPyConnection,
    rows: list[dict[str, Any]],
    s: Sources,
    prefixes: list[tuple[str, str]],
    start: dt.datetime | None,
    end: dt.datetime | None,
) -> None:
    """In-place: attach the distinct editors each returned user used on their matched changesets, from the
    rollup (history) and the base changesets (recent). Display-only, so scoped to the returned page."""
    for r in rows:
        r["editors"] = []
    if not rows:
        return
    uids = [r["uid"] for r in rows]
    ph = ", ".join("?" for _ in uids)
    window_sql, window_params = catalog._window_clause(start, end)
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
    hist = (
        f"SELECT DISTINCT uid, editor FROM {s.history_rel} "
        f"WHERE ({hist_pred}) AND created_at < ?{window_sql} AND uid IN ({ph})"
    )
    hist_params = [*prefix_params, s.frontier, *window_params, *uids]
    if s.pg_attach:
        rrel = catalog.recent_user_editors(
            s.pg_attach, uids, prefixes=prefixes, frontier=s.frontier, start=start, end=end
        )
        recent, recent_params = f"SELECT uid, editor FROM {rrel}", []
    else:
        recent_pred = " OR ".join("(lower(x) >= ? AND lower(x) < ?)" for _ in prefixes)
        recent = (
            f"SELECT DISTINCT uid, editor FROM {s.recent_changesets_rel} "
            f"WHERE created_at >= ?{window_sql} AND uid IN ({ph}) "
            f"AND EXISTS (SELECT 1 FROM UNNEST(hashtags) AS t(x) WHERE {recent_pred})"
        )
        recent_params = [s.frontier, *window_params, *uids, *prefix_params]
    result = con.execute(
        f"SELECT uid, list(DISTINCT editor) AS editors FROM ({hist} UNION ALL {recent}) GROUP BY uid",
        [*hist_params, *recent_params],
    ).fetchall()
    by_uid = {uid: list(editors or []) for uid, editors in result}
    for r in rows:
        r["editors"] = by_uid.get(r["uid"], [])


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
    exact: bool = False,
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
    prefixes = _prefixes(hashtag, exact=exact)
    s = _with_recent_tail_cache(con, s, prefixes, start, end)
    hsql, hp = _history(s, prefixes, start, end)
    count_sql, count_params = catalog.history_scope_count(
        s.history_rel, prefixes=prefixes, frontier=s.frontier, start=start, end=end
    )
    count_row = con.execute(count_sql, count_params).fetchone()
    small_history = (count_row[0] if count_row else 0) <= _MAX_TAG_ROWS
    # Counts stream narrow (no tags struct through the dedup) so the aggregate stays cheap; per-user tags
    # are attached for the returned page alone. All-time memoizes the aggregate.
    _q_lb_agg = f"SELECT uid, count(*) AS changesets, {_SUM_AS}"
    _materialize_history(
        con, "_q_lb", f"{_q_lb_agg} FROM ({hsql}) GROUP BY uid", hp, _cache_path(s, prefixes, "lb_users", start, end)
    )
    rrel, rp = _recent_users(s, prefixes, start, end)
    search_pred, search_params = "", []
    if q:
        search_pred = " WHERE lower(name) LIKE ? ESCAPE '\\'"
        search_params = [f"%{_like_escape(q.strip().lower())}%"]
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _lb_agg AS
        WITH pu AS (
            SELECT uid, changesets, {_COLS} FROM _q_lb
            UNION ALL
            SELECT uid, changesets, {_COLS} FROM {rrel}
        ),
        c AS (
            SELECT uid, SUM(changesets) AS changesets, {_SUM_AS} FROM pu GROUP BY uid
        )
        SELECT c.uid, COALESCE(u.username, 'user ' || c.uid) AS name, c.changesets, {_COLS},
               {map_changes_expr("c")} AS map_changes
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
    if small_history:
        _attach_user_tags(con, rows, s, prefixes, start, end)
        tags_gated = False
    elif _attach_user_tags_cached(con, rows, s, prefixes, start, end):
        tags_gated = False
    else:
        for r in rows:
            r["tag_stats"] = {}
        tags_gated = True
    _attach_user_editors(con, rows, s, prefixes, start, end)
    _attach_user_hashtags(con, rows, s, prefixes, start, end)
    return {
        "items": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
        "tags_gated": tags_gated,
    }


def tags(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    exact: bool = False,
    limit: int = 100,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Tag key/value breakdown (creates/modifies/length) over the hashtag's deduped changesets. Optional
    [start, end) window."""
    prefixes = _prefixes(hashtag, exact=exact)
    s = _with_recent_tail_cache(con, s, prefixes, start, end)
    per_tag = (
        "SELECT t.k AS k, t.v AS v, SUM(t.c) AS creates, SUM(t.m) AS modifies, SUM(t.l) AS length_m "
        "FROM (SELECT UNNEST(tags) AS t FROM {rel}) GROUP BY t.k, t.v"
    )
    hist_tag_sql, hp = catalog.history_tag_agg(
        s.history_rel, prefixes=prefixes, frontier=s.frontier, start=start, end=end
    )
    _materialize_history(con, "_q_tg", hist_tag_sql, hp, _cache_path(s, prefixes, "tags", start, end))
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
    exact: bool = False,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Editor breakdown for the hashtag: changesets, distinct users, and map_changes per editor. Optional
    [start, end) window. Aggregated per (editor, uid) on each side so distinct users are exact across the
    frontier."""
    prefixes = _prefixes(hashtag, exact=exact)
    s = _with_recent_tail_cache(con, s, prefixes, start, end)
    hsql, hp = _history(s, prefixes, start, end)
    per_eu = (
        "SELECT COALESCE(NULLIF(editor, ''), 'unknown') AS editor, uid, count(*) AS cs, "
        f"{map_changes_sum()} FROM {{rel}} GROUP BY 1, uid"
    )
    _materialize_history(
        con, "_q_ed", per_eu.format(rel=f"({hsql})"), hp, _cache_path(s, prefixes, "editors", start, end)
    )
    if _use_pg(s):
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
    exact: bool = False,
    limit: int = 15,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Hashtags carried by the changesets that match the search (co-occurring), ranked by distinct
    contributors then total edits: `[{hashtag, users, edits}]`. Reveals which other hashtags and
    projects were used alongside the searched hashtag. Optional [start, end) window."""
    prefixes = _prefixes(hashtag, exact=exact)
    s = _with_recent_tail_cache(con, s, prefixes, start, end)
    window_sql, window_params = catalog._window_clause(start, end)
    prefix_params = [bound for pair in prefixes for bound in pair]
    hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)

    parts: list[str] = []
    params: list[object] = []

    # History side: co-occurring hashtags via a changeset_id self-join. All-time reads the warmed cache when
    # present; otherwise a cheap count gates it so a mega-hashtag does not scan the whole rollup.
    cooccur_path = _cache_path(s, prefixes, "cooccur", start, end)
    if cooccur_path is not None and cooccur_path.exists():
        parts.append(f"SELECT hashtag, uid, mc FROM read_parquet('{cooccur_path.as_posix()}')")
    else:
        hc_sql, hc_params = catalog.history_scope_count(
            s.history_rel, prefixes=prefixes, frontier=s.frontier, start=start, end=end
        )
        hc_row = con.execute(hc_sql, hc_params).fetchone()
        hist_count = hc_row[0] if hc_row else 0
        if 0 < hist_count <= _MAX_TAG_ROWS and _cooccur_history_cheap(con, s, start, end):
            matched_cs = (
                f"SELECT DISTINCT changeset_id FROM {s.history_rel} WHERE ({hist_pred}) AND created_at < ?{window_sql}"
            )
            parts.append(
                f"SELECT h.hashtag AS hashtag, h.uid AS uid, {map_changes_expr('h')} AS mc "
                f"FROM {s.history_rel} h JOIN ({matched_cs}) m USING (changeset_id) WHERE h.created_at < ?{window_sql}"
            )
            params += [*prefix_params, s.frontier, *window_params, s.frontier, *window_params]

    # Recent side: every hashtag carried by a matched changeset in the live tail.
    if _use_pg(s):
        rrel = catalog.recent_cooccur_agg(s.pg_attach, prefixes=prefixes, frontier=s.frontier, start=start, end=end)
        parts.append(f"SELECT hashtag, uid, mc FROM {rrel}")
    else:
        recent_match = " OR ".join("(lower(x) >= ? AND lower(x) < ?)" for _ in prefixes)
        parts.append(
            f"SELECT lower(h) AS hashtag, uid, mc FROM (SELECT c.uid, UNNEST(c.hashtags) AS h, cs.mc AS mc "
            f"FROM {s.recent_changesets_rel} c JOIN (SELECT changeset_id, {map_changes_sum(alias='mc')} "
            f"FROM {s.recent_stats_rel} GROUP BY changeset_id) cs USING (changeset_id) "
            f"WHERE c.created_at >= ?{window_sql} AND EXISTS "
            f"(SELECT 1 FROM UNNEST(c.hashtags) AS t(x) WHERE {recent_match}))"
        )
        params += [s.frontier, *window_params, *prefix_params]

    params.append(limit)
    res = con.execute(
        f"SELECT hashtag, count(DISTINCT uid) AS users, COALESCE(SUM(mc), 0) AS edits "
        f"FROM ({' UNION ALL '.join(parts)}) GROUP BY hashtag ORDER BY users DESC, hashtag ASC LIMIT ?",
        params,
    )
    return _rows(res)


TREND_INTERVALS = ("day", "week", "month")


def trends(
    con: duckdb.DuckDBPyConnection,
    hashtag: str | list[str],
    s: Sources,
    *,
    exact: bool = False,
    interval: str = "day",
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Per-bucket activity for the hashtag: changesets, distinct users, and map_changes per day, week, or
    month (UTC). Optional [start, end) window. Buckets are disjoint at the frontier, so per-side bucket
    aggregates combine additively."""
    if interval not in TREND_INTERVALS:
        raise ValueError(f"interval must be one of {TREND_INTERVALS}")
    prefixes = _prefixes(hashtag, exact=exact)
    s = _with_recent_tail_cache(con, s, prefixes, start, end)
    hsql, hp = _history(s, prefixes, start, end)
    bucket = f"CAST(date_trunc('{interval}', created_at) AS DATE)::VARCHAR"
    per_bucket = (
        f"SELECT {bucket} AS bucket, count(*) AS changesets, count(DISTINCT uid) AS users, {map_changes_sum()} "
        "FROM {rel} GROUP BY 1"
    )
    _materialize_history(
        con, "_q_tr", per_bucket.format(rel=f"({hsql})"), hp, _cache_path(s, prefixes, f"trends_{interval}", start, end)
    )
    if _use_pg(s):
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
    exact: bool = False,
    limit: int = 2000,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Changeset centroids `(changeset_id, uid, lon, lat)` for the hashtag union, up to `limit`, for a
    map. Optional [start, end) window. Recent centroids come from the base changesets bbox; history
    centroids come from the rollup only when it carries `lon`/`lat` (older published rollups do not)."""
    prefixes = _prefixes(hashtag, exact=exact)
    if s.pg_attach:
        rrel = catalog.recent_map_agg(s.pg_attach, prefixes=prefixes, frontier=s.frontier, start=start, end=end)
        recent_sel = f"SELECT changeset_id, uid, lon, lat FROM {rrel}"
        history_cols = {d[0] for d in con.execute(f"SELECT * FROM {s.history_rel} LIMIT 0").description}
        if "lon" in history_cols and "lat" in history_cols:
            window_sql, window_params = catalog._window_clause(start, end)
            prefix_params = [bound for pair in prefixes for bound in pair]
            hist_pred = " OR ".join("(hashtag >= ? AND hashtag < ?)" for _ in prefixes)
            hist = (
                f"SELECT changeset_id, uid, lon, lat FROM {s.history_rel} "
                f"WHERE ({hist_pred}) AND created_at < ?{window_sql} AND lon IS NOT NULL"
            )
            sql = f"SELECT DISTINCT ON (changeset_id) changeset_id, uid, lon, lat FROM ({hist} UNION ALL {recent_sel})"
            res = con.execute(f"{sql} LIMIT ?", [*prefix_params, s.frontier, *window_params, limit])
        else:
            sql = f"SELECT DISTINCT ON (changeset_id) changeset_id, uid, lon, lat FROM ({recent_sel})"
            res = con.execute(f"{sql} LIMIT ?", [limit])
        return _rows(res)
    sql, params = map_scope(
        s.history_rel, s.recent_changesets_rel, prefixes=prefixes, frontier=s.frontier, start=start, end=end
    )
    res = con.execute(f"{sql} LIMIT ?", [*params, limit])
    return _rows(res)


# Whole-OSM (no-hashtag) stats over a recent window from Postgres, capped at GLOBAL_MAX_DAYS since the
# aggregate scans every changeset in the window.
GLOBAL_MAX_DAYS = int(os.getenv("OSMSG_GLOBAL_MAX_DAYS", "7"))

_PC_SUMS = ", ".join(f"SUM(s.{c}) AS {c}" for c in COUNT_COLS)

# Above this per-page map_changes sum, skip the per-user tag unnest (its cost tracks map_changes).
_MAX_GLOBAL_TAG_MAP_CHANGES = 500_000


def _global_users_rel(s: Sources, start, end) -> tuple[str, list[object]]:
    if s.pg_attach:
        return catalog.global_user_agg(s.pg_attach, start=start, end=end), []
    return (
        f"(SELECT s.uid AS uid, count(DISTINCT s.changeset_id) AS changesets, {_PC_SUMS} "
        f"FROM {s.recent_stats_rel} s JOIN {s.recent_changesets_rel} c USING (changeset_id) "
        f"WHERE c.created_at >= ? AND c.created_at < ? GROUP BY s.uid)"
    ), [start, end]


def _attach_global_user_tags(con, rows: list[dict[str, Any]], s: Sources, start, end) -> None:
    for r in rows:
        r["tag_stats"] = {}
    if not rows:
        return
    uids = [r["uid"] for r in rows]
    if s.pg_attach:
        rrel = catalog.global_user_tags(s.pg_attach, uids, start=start, end=end)
        tag_rows = con.execute(f"SELECT uid, k, v, c, m, l FROM {rrel}").fetchall()
    else:
        ph = ", ".join("?" for _ in uids)
        tag_rows = con.execute(
            "SELECT uid, t.k AS k, t.v AS v, SUM(t.c) AS c, SUM(t.m) AS m, SUM(t.l) AS l "
            f"FROM (SELECT s.uid AS uid, UNNEST(s.tags) AS t FROM {s.recent_stats_rel} s "
            f"JOIN {s.recent_changesets_rel} c USING (changeset_id) "
            f"WHERE c.created_at >= ? AND c.created_at < ? AND s.uid IN ({ph})) GROUP BY uid, t.k, t.v",
            [start, end, *uids],
        ).fetchall()
    per_uid: dict[Any, list[dict[str, Any]]] = {}
    for uid, k, v, c, m, length_m in tag_rows:
        per_uid.setdefault(uid, []).append({"k": k, "v": v, "c": c, "m": m, "l": length_m})
    for r in rows:
        if per_uid.get(r["uid"]):
            r["tag_stats"] = _tags_to_nested(per_uid[r["uid"]])


def _attach_global_user_editors(con, rows: list[dict[str, Any]], s: Sources, start, end) -> None:
    for r in rows:
        r["editors"] = []
    if not rows:
        return
    uids = [r["uid"] for r in rows]
    if s.pg_attach:
        recent, params = (
            f"SELECT uid, editor FROM {catalog.global_user_editors(s.pg_attach, uids, start=start, end=end)}",
            [],
        )
    else:
        ph = ", ".join("?" for _ in uids)
        recent = (
            f"SELECT DISTINCT s.uid AS uid, c.editor AS editor FROM {s.recent_stats_rel} s "
            f"JOIN {s.recent_changesets_rel} c USING (changeset_id) "
            f"WHERE c.created_at >= ? AND c.created_at < ? AND s.uid IN ({ph})"
        )
        params = [start, end, *uids]
    result = con.execute(
        f"SELECT uid, list(DISTINCT editor) AS editors FROM ({recent}) GROUP BY uid", params
    ).fetchall()
    by_uid = {uid: list(editors or []) for uid, editors in result}
    for r in rows:
        r["editors"] = by_uid.get(r["uid"], [])


def _attach_global_user_hashtags(con, rows: list[dict[str, Any]], s: Sources, start, end) -> None:
    for r in rows:
        r["hashtags"] = []
    if not rows:
        return
    uids = [r["uid"] for r in rows]
    if s.pg_attach:
        recent, params = (
            f"SELECT uid, hashtag FROM {catalog.global_user_hashtags(s.pg_attach, uids, start=start, end=end)}",
            [],
        )
    else:
        ph = ", ".join("?" for _ in uids)
        recent = (
            f"SELECT DISTINCT uid, lower(h) AS hashtag FROM (SELECT s.uid AS uid, UNNEST(c.hashtags) AS h "
            f"FROM {s.recent_stats_rel} s JOIN {s.recent_changesets_rel} c USING (changeset_id) "
            f"WHERE c.created_at >= ? AND c.created_at < ? AND s.uid IN ({ph}))"
        )
        params = [start, end, *uids]
    result = con.execute(
        f"SELECT uid, list(DISTINCT hashtag) AS hashtags FROM ({recent}) GROUP BY uid", params
    ).fetchall()
    by_uid = {uid: list(hashtags or []) for uid, hashtags in result}
    for r in rows:
        r["hashtags"] = by_uid.get(r["uid"], [])


def global_tags(con: duckdb.DuckDBPyConnection, s: Sources, *, start, end, limit: int = 100) -> list[dict[str, Any]]:
    if s.pg_attach:
        rrel = catalog.global_tag_agg(s.pg_attach, start=start, end=end)
        res = con.execute(
            f"SELECT k AS tag_key, v AS tag_value, creates, modifies, length_m "
            f"FROM {rrel} ORDER BY creates DESC LIMIT ?",
            [limit],
        )
    else:
        res = con.execute(
            "SELECT k AS tag_key, v AS tag_value, SUM(c) AS creates, SUM(m) AS modifies, SUM(l) AS length_m "
            f"FROM (SELECT t.k AS k, t.v AS v, t.c AS c, t.m AS m, t.l AS l FROM (SELECT UNNEST(s.tags) AS t "
            f"FROM {s.recent_stats_rel} s JOIN {s.recent_changesets_rel} c USING (changeset_id) "
            f"WHERE c.created_at >= ? AND c.created_at < ?)) GROUP BY k, v ORDER BY creates DESC LIMIT ?",
            [start, end, limit],
        )
    return _rows(res)


def global_summary(con: duckdb.DuckDBPyConnection, s: Sources, *, start, end) -> dict[str, Any]:
    rel, params = _global_users_rel(s, start, end)
    res = con.execute(
        f"SELECT count(*) AS users, COALESCE(SUM(changesets), 0) AS changesets, {sum_cols()}, {map_changes_sum()} "
        f"FROM {rel}",
        params,
    )
    return _rows(res)[0]


def global_leaderboard(
    con: duckdb.DuckDBPyConnection,
    s: Sources,
    *,
    start,
    end,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort: str = "map_changes",
    order: str = "desc",
    q: str | None = None,
) -> dict[str, Any]:
    if sort not in LEADERBOARD_SORTS:
        raise ValueError(f"sort must be one of {tuple(LEADERBOARD_SORTS)}")
    if order not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    rel, params = _global_users_rel(s, start, end)
    search_pred, search_params = "", []
    if q:
        search_pred = " WHERE lower(name) LIKE ? ESCAPE '\\'"
        search_params = [f"%{_like_escape(q.strip().lower())}%"]
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _g_lb AS
        SELECT c.uid, COALESCE(u.username, 'user ' || c.uid) AS name, c.changesets, {_COLS},
               {map_changes_expr("c")} AS map_changes
        FROM {rel} c LEFT JOIN {s.users_rel} u USING (uid){search_pred}
        """,
        [*params, *search_params],
    )
    total_row = con.execute("SELECT count(*) FROM _g_lb").fetchone()
    total = total_row[0] if total_row else 0
    offset = (page - 1) * page_size
    order_by = f"{LEADERBOARD_SORTS[sort]} {order.upper()} NULLS LAST, uid ASC"
    rows = _rows(con.execute(f"SELECT * FROM _g_lb ORDER BY {order_by} LIMIT ? OFFSET ?", [page_size, offset]))
    for i, r in enumerate(rows):
        r["rank"] = offset + i + 1
    # Skip the per-user tag unnest for heavy pages (cost tracks map_changes); /tags still covers the aggregate.
    if sum(r.get("map_changes") or 0 for r in rows) <= _MAX_GLOBAL_TAG_MAP_CHANGES:
        _attach_global_user_tags(con, rows, s, start, end)
    else:
        for r in rows:
            r["tag_stats"] = {}
    _attach_global_user_editors(con, rows, s, start, end)
    _attach_global_user_hashtags(con, rows, s, start, end)
    return {
        "items": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
    }


def global_editors(con: duckdb.DuckDBPyConnection, s: Sources, *, start, end) -> list[dict[str, Any]]:
    if s.pg_attach:
        rel = catalog.global_editor_agg(s.pg_attach, start=start, end=end)
        eu_sel, params = f"SELECT editor, uid, cs, map_changes FROM {rel}", []
    else:
        eu_sel = (
            f"SELECT COALESCE(NULLIF(c.editor, ''), 'unknown') AS editor, s.uid AS uid, "
            f"count(DISTINCT s.changeset_id) AS cs, sum({map_changes_expr('s')}) AS map_changes "
            f"FROM {s.recent_stats_rel} s JOIN {s.recent_changesets_rel} c USING (changeset_id) "
            f"WHERE c.created_at >= ? AND c.created_at < ? GROUP BY 1, s.uid"
        )
        params = [start, end]
    res = con.execute(
        f"SELECT editor, SUM(cs) AS changesets, count(*) AS users, SUM(map_changes) AS map_changes "
        f"FROM ({eu_sel}) GROUP BY editor ORDER BY map_changes DESC",
        params,
    )
    return _rows(res)


def global_trending(con: duckdb.DuckDBPyConnection, s: Sources, *, start, end, limit: int = 15) -> list[dict[str, Any]]:
    if s.pg_attach:
        rel = catalog.global_trending(s.pg_attach, start=start, end=end)
        res = con.execute(
            f"SELECT hashtag, changesets, users FROM {rel} ORDER BY users DESC, hashtag ASC LIMIT ?", [limit]
        )
    else:
        res = con.execute(
            "SELECT hashtag, count(DISTINCT changeset_id) AS changesets, count(DISTINCT uid) AS users FROM ("
            f"SELECT c.changeset_id, c.uid, lower(UNNEST(c.hashtags)) AS hashtag FROM {s.recent_changesets_rel} c "
            "WHERE c.created_at >= ? AND c.created_at < ?) GROUP BY hashtag ORDER BY users DESC, hashtag ASC LIMIT ?",
            [start, end, limit],
        )
    return _rows(res)
