"""DuckDB-on-top query layer: answers hashtag stats from the published hashtag_changeset artifact (history)
plus the recent tail derived on the fly from the base Postgres tables, so the API matches the CLI. No
materialized recent rollup; the split frontier is re-read on a TTL so a newly published month needs no restart.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import datetime as dt
import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import duckdb
from litestar.exceptions import HTTPException, TooManyRequestsException

from osmsg import query
from osmsg.db.schema import _apply_runtime_pragmas
from osmsg.history import fetch_manifest
from osmsg.query import Sources

_log = logging.getLogger("osmsg.api.duck")
_PG_ATTACH = "pg"

# Warm connections (extensions + Postgres attached) reused across requests so cold-start is paid once;
# the pool size caps concurrency, so heavy queries queue instead of thrashing the CPU.
_POOL_SIZE = int(os.getenv("OSMSG_DUCKDB_POOL", "3"))
# Max wait for a free slot before shedding with 429 (see _acquire).
_POOL_WAIT = float(os.getenv("OSMSG_POOL_WAIT_SECONDS", "8"))
# Above the slowest legitimate query; a watchdog interrupts anything past it so a disconnected client or
# runaway cannot pin a core.
_QUERY_TIMEOUT = float(os.getenv("OSMSG_QUERY_TIMEOUT_SECONDS", "150"))
_pool: queue.Queue | None = None
_pool_lock = threading.Lock()

_QUERY_CACHE_DIR = os.getenv("OSMSG_QUERY_CACHE_DIR")  # memoize all-time mega aggregates here; unset -> off
_HISTORY_URL = os.getenv("OSMSG_HISTORY_URL", "hf://datasets/kshitijrajsharma/osmsg-history")
_ROLLUP = os.getenv("OSMSG_ROLLUP_BASE", f"{_HISTORY_URL}/rollup")
_HASHTAG_CHANGESET = os.getenv("OSMSG_HASHTAG_CHANGESET", f"{_ROLLUP}/hashtag_changeset/data.parquet")
_USERS = os.getenv("OSMSG_USERS", f"{_ROLLUP}/users/data.parquet")
_FRONTIER_TTL_SECONDS = int(os.getenv("OSMSG_FRONTIER_TTL_SECONDS", "3600"))
# work_mem for the API's own pooled Postgres connections only (not the worker, not the db global config), so
# the heaviest global aggregate stays mostly in memory instead of spilling. Sized to the pool + db cap.
_PG_WORK_MEM = os.getenv("OSMSG_PG_WORK_MEM", "64MB")

_frontier_cache: tuple[float, dt.datetime] | None = None


def _libpq_dsn() -> str:
    """DuckDB's postgres extension wants a libpq keyword string; the app config is a postgresql:// URL."""
    u = urlparse(os.environ["DATABASE_URL"])
    parts = {
        "host": u.hostname,
        "port": u.port,
        "dbname": u.path.lstrip("/"),
        "user": u.username,
        "password": u.password,
    }
    dsn = " ".join(f"{k}={v}" for k, v in parts.items() if v is not None)
    return f"{dsn} options='-c work_mem={_PG_WORK_MEM}'"


def _frontier() -> dt.datetime:
    """The published frontier, re-read from the manifest at most once per TTL so a newly published
    month advances the split without restarting the API."""
    global _frontier_cache
    now = time.monotonic()
    if _frontier_cache is not None and now - _frontier_cache[0] < _FRONTIER_TTL_SECONDS:
        return _frontier_cache[1]
    manifest = fetch_manifest(_HISTORY_URL)
    if manifest is None:
        raise RuntimeError("history manifest unavailable; set OSMSG_HISTORY_URL")
    _frontier_cache = (now, manifest.frontier)
    return manifest.frontier


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL json; LOAD json; INSTALL postgres; LOAD postgres;")
    con.execute("SET http_retries=10;")
    # Memory/temp pragmas so concurrent pooled queries cannot sum past the container memory cap.
    _apply_runtime_pragmas(con)
    con.execute(f"ATTACH '{_libpq_dsn().replace(chr(39), chr(39) * 2)}' AS pg (TYPE postgres, READ_ONLY)")
    return con


def _sources() -> Sources:
    return Sources(
        history_rel=f"read_parquet('{_HASHTAG_CHANGESET}')",
        recent_stats_rel="pg.changeset_stats",
        recent_changesets_rel="pg.changesets",
        frontier=_frontier(),
        users_rel=f"read_parquet('{_USERS}')",
        pg_attach=_PG_ATTACH,
        cache_dir=_QUERY_CACHE_DIR,
    )


def _pool_ready() -> queue.Queue:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                warm: queue.Queue = queue.Queue()
                for _ in range(_POOL_SIZE):
                    warm.put(_connect())
                _pool = warm
    return _pool


def _acquire(pool: queue.Queue) -> duckdb.DuckDBPyConnection:
    """Borrow a warm connection, waiting at most _POOL_WAIT for a free slot. Past that the box is saturated,
    so shed the request with 429 instead of queueing to the watchdog timeout or stacking toward an OOM."""
    try:
        return pool.get(timeout=_POOL_WAIT)
    except queue.Empty:
        raise TooManyRequestsException(detail="Server at capacity; retry shortly.") from None


# A timed-out all-time query caches nothing, so its retry is just as slow; on timeout we re-run it in the
# background to fill the cache the retry reads. One at a time on its own connection so it can't starve the pool.
_warm_pool = ThreadPoolExecutor(max_workers=1)
_warm_inflight: set[tuple[str, str, bool]] = set()
_warm_lock = threading.Lock()


def _warm(fn, hashtag, kwargs, key) -> None:
    con = _connect()
    try:
        fn(con, hashtag, _sources(), **kwargs)
    except duckdb.Error as e:
        _log.warning("cache warm rerun of %s for %r failed: %s", fn.__name__, hashtag, e)
    finally:
        con.close()
        with _warm_lock:
            _warm_inflight.discard(key)


def _enqueue_warm(fn, hashtag, kwargs) -> None:
    """Re-run an all-time query off the request path to fill its cache, deduped per key, best-effort.
    No-op when caching is off."""
    if _QUERY_CACHE_DIR is None:
        return
    key = (fn.__name__, repr(hashtag), bool(kwargs.get("exact", False)))
    with _warm_lock:
        if key in _warm_inflight:
            return
        _warm_inflight.add(key)
    _warm_pool.submit(_warm, fn, hashtag, kwargs, key)


def _warm_all_time_job(hashtag, key, exact) -> None:
    con = _connect()
    try:
        query.warm_all_time(con, hashtag, _sources(), exact=exact)
    except duckdb.Error as e:
        _log.warning("all-time warm for %r failed: %s", hashtag, e)
    finally:
        con.close()
        with _warm_lock:
            _warm_inflight.discard(key)


def _maybe_warm_all_time(hashtag, exact) -> None:
    """Fire a background warm of the all-time history caches (leaderboard per-user tags, trending
    co-occurring) for this hashtag when any is still cold, deduped per hashtag. No-op when caching is off."""
    if _QUERY_CACHE_DIR is None or not query.all_time_warm_pending(_sources(), hashtag, exact=exact):
        return
    key = ("warm_all_time", repr(hashtag), bool(exact))
    with _warm_lock:
        if key in _warm_inflight:
            return
        _warm_inflight.add(key)
    _warm_pool.submit(_warm_all_time_job, hashtag, key, exact)


def _run(fn, hashtag, **kwargs):
    """Borrow a warm pooled connection (429 past _POOL_WAIT caps concurrency), run under a watchdog that
    interrupts a query past _QUERY_TIMEOUT, and return it (replaced if it errored or was interrupted)."""
    pool = _pool_ready()
    con = _acquire(pool)
    done = threading.Event()
    interrupted = False

    def _watchdog() -> None:
        nonlocal interrupted
        if not done.wait(_QUERY_TIMEOUT):
            interrupted = True
            with contextlib.suppress(duckdb.Error):
                con.interrupt()

    watcher = threading.Thread(target=_watchdog, daemon=True)
    watcher.start()
    healthy = True
    try:
        return fn(con, hashtag, _sources(), **kwargs)
    except duckdb.Error:
        healthy = False  # interrupted or DB error -> the connection may be dirty, recycle it
        if interrupted:
            # Only all-time queries hit the shared cache, so only they are worth warming.
            if kwargs.get("start") is None and kwargs.get("end") is None:
                _enqueue_warm(fn, hashtag, kwargs)
            raise HTTPException(status_code=503, detail="Server is busy, please try again in a moment.") from None
        raise
    finally:
        done.set()
        watcher.join()  # ensure no interrupt is still pending before the connection is reused
        if healthy:
            pool.put(con)
        else:
            with contextlib.suppress(duckdb.Error):
                con.close()
            pool.put(_connect())


async def summary(
    hashtag: str | list[str],
    *,
    exact: bool = False,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
):
    return await asyncio.to_thread(_run, query.summary, hashtag, exact=exact, start=start, end=end)


async def leaderboard(
    hashtag: str | list[str],
    *,
    exact: bool = False,
    page: int = 1,
    page_size: int = query.DEFAULT_PAGE_SIZE,
    sort: str = "map_changes",
    order: str = "desc",
    q: str | None = None,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
):
    res = await asyncio.to_thread(
        _run,
        query.leaderboard,
        hashtag,
        exact=exact,
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
        q=q,
        start=start,
        end=end,
    )
    if start is None and end is None:
        _maybe_warm_all_time(hashtag, exact)
    return res


async def tags(
    hashtag: str | list[str],
    *,
    exact: bool = False,
    limit: int = 100,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
):
    return await asyncio.to_thread(_run, query.tags, hashtag, exact=exact, limit=limit, start=start, end=end)


async def editors(
    hashtag: str | list[str],
    *,
    exact: bool = False,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
):
    return await asyncio.to_thread(_run, query.editors, hashtag, exact=exact, start=start, end=end)


async def hashtags(
    hashtag: str | list[str],
    *,
    exact: bool = False,
    limit: int = 15,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
):
    res = await asyncio.to_thread(_run, query.hashtags, hashtag, exact=exact, limit=limit, start=start, end=end)
    if start is None and end is None:
        _maybe_warm_all_time(hashtag, exact)
    return res


async def trends(
    hashtag: str | list[str],
    *,
    exact: bool = False,
    interval: str = "day",
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
):
    return await asyncio.to_thread(_run, query.trends, hashtag, exact=exact, interval=interval, start=start, end=end)


async def map_points(
    hashtag: str | list[str],
    *,
    exact: bool = False,
    limit: int = 2000,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
):
    return await asyncio.to_thread(_run, query.map_points, hashtag, exact=exact, limit=limit, start=start, end=end)


def _run_global(fn, **kwargs):
    """Like _run but for the no-hashtag global endpoints: runs fn(con, _sources(), **kwargs) under the
    watchdog on a pooled connection. No warm path (global windows are recent, uncached)."""
    pool = _pool_ready()
    con = _acquire(pool)
    done = threading.Event()
    interrupted = False

    def _watchdog() -> None:
        nonlocal interrupted
        if not done.wait(_QUERY_TIMEOUT):
            interrupted = True
            with contextlib.suppress(duckdb.Error):
                con.interrupt()

    watcher = threading.Thread(target=_watchdog, daemon=True)
    watcher.start()
    healthy = True
    try:
        return fn(con, _sources(), **kwargs)
    except duckdb.Error:
        healthy = False
        if interrupted:
            raise HTTPException(status_code=503, detail="Server is busy, please try again in a moment.") from None
        raise
    finally:
        done.set()
        watcher.join()
        if healthy:
            pool.put(con)
        else:
            with contextlib.suppress(duckdb.Error):
                con.close()
            pool.put(_connect())


# Memoize whole-OSM results by a grain-rounded window so repeat hits are instant.
_GLOBAL_CACHE_TTL = float(os.getenv("OSMSG_GLOBAL_CACHE_TTL", "120"))
_GLOBAL_GRAIN = 60
_global_cache: collections.OrderedDict[tuple, tuple[float, object]] = collections.OrderedDict()
_global_cache_lock = threading.Lock()


def _round_window(start: dt.datetime, end: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    def floor(t: dt.datetime) -> dt.datetime:
        return t.replace(second=(t.second // _GLOBAL_GRAIN) * _GLOBAL_GRAIN, microsecond=0)

    return floor(start), floor(end)


async def _global_cached(fn, key_extra, start, end, **kwargs):
    rs, re = _round_window(start, end)
    key = (fn.__name__, rs, re, key_extra)
    now = time.monotonic()
    with _global_cache_lock:
        hit = _global_cache.get(key)
        if hit and hit[0] > now:
            _global_cache.move_to_end(key)
            return hit[1]
    result = await asyncio.to_thread(_run_global, fn, start=rs, end=re, **kwargs)
    with _global_cache_lock:
        _global_cache[key] = (now + _GLOBAL_CACHE_TTL, result)
        while len(_global_cache) > 512:
            _global_cache.popitem(last=False)
    return result


async def global_summary(*, start: dt.datetime, end: dt.datetime):
    return await _global_cached(query.global_summary, None, start, end)


async def global_leaderboard(
    *,
    start: dt.datetime,
    end: dt.datetime,
    page: int = 1,
    page_size: int = query.DEFAULT_PAGE_SIZE,
    sort: str = "map_changes",
    order: str = "desc",
    q: str | None = None,
):
    return await _global_cached(
        query.global_leaderboard,
        (page, page_size, sort, order, q),
        start,
        end,
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
        q=q,
    )


async def global_editors(*, start: dt.datetime, end: dt.datetime):
    return await _global_cached(query.global_editors, None, start, end)


async def global_tags(*, start: dt.datetime, end: dt.datetime, limit: int = 100):
    return await _global_cached(query.global_tags, limit, start, end, limit=limit)


async def global_trending(*, start: dt.datetime, end: dt.datetime, limit: int = 15):
    return await _global_cached(query.global_trending, limit, start, end, limit=limit)
