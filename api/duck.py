"""DuckDB-on-top query layer: answers hashtag stats from the published hashtag_changeset artifact (history)
plus the recent tail derived on the fly from the base Postgres tables, so the API matches the CLI. No
materialized recent rollup; the split frontier is re-read on a TTL so a newly published month needs no restart.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from urllib.parse import urlparse

import duckdb

from osmsg import query
from osmsg.history import fetch_manifest
from osmsg.query import Sources

_HISTORY_URL = os.getenv("OSMSG_HISTORY_URL", "hf://datasets/kshitijrajsharma/osmsg-history")
_ROLLUP = os.getenv("OSMSG_ROLLUP_BASE", f"{_HISTORY_URL}/rollup")
_HASHTAG_CHANGESET = os.getenv("OSMSG_HASHTAG_CHANGESET", f"{_ROLLUP}/hashtag_changeset/data.parquet")
_USERS = os.getenv("OSMSG_USERS", f"{_ROLLUP}/users/data.parquet")
_FRONTIER_TTL_SECONDS = int(os.getenv("OSMSG_FRONTIER_TTL_SECONDS", "3600"))

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
    return " ".join(f"{k}={v}" for k, v in parts.items() if v is not None)


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
    con.execute(f"ATTACH '{_libpq_dsn()}' AS pg (TYPE postgres, READ_ONLY)")
    return con


def _sources() -> Sources:
    return Sources(
        history_rel=f"read_parquet('{_HASHTAG_CHANGESET}')",
        recent_stats_rel="pg.changeset_stats",
        recent_changesets_rel="pg.changesets",
        frontier=_frontier(),
        users_rel=f"read_parquet('{_USERS}')",
    )


def _run(fn, hashtag, **kwargs):
    con = _connect()
    try:
        return fn(con, hashtag, _sources(), **kwargs)
    finally:
        con.close()


async def summary(hashtag: str | list[str], *, start: dt.datetime | None = None, end: dt.datetime | None = None):
    return await asyncio.to_thread(_run, query.summary, hashtag, start=start, end=end)


async def leaderboard(
    hashtag: str | list[str],
    *,
    page: int = 1,
    page_size: int = query.DEFAULT_PAGE_SIZE,
    sort: str = "map_changes",
    order: str = "desc",
    q: str | None = None,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
):
    return await asyncio.to_thread(
        _run,
        query.leaderboard,
        hashtag,
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
        q=q,
        start=start,
        end=end,
    )


async def tags(
    hashtag: str | list[str], *, limit: int = 100, start: dt.datetime | None = None, end: dt.datetime | None = None
):
    return await asyncio.to_thread(_run, query.tags, hashtag, limit=limit, start=start, end=end)


async def editors(hashtag: str | list[str], *, start: dt.datetime | None = None, end: dt.datetime | None = None):
    return await asyncio.to_thread(_run, query.editors, hashtag, start=start, end=end)


async def hashtags(
    hashtag: str | list[str], *, limit: int = 15, start: dt.datetime | None = None, end: dt.datetime | None = None
):
    return await asyncio.to_thread(_run, query.hashtags, hashtag, limit=limit, start=start, end=end)


async def trends(
    hashtag: str | list[str],
    *,
    interval: str = "day",
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
):
    return await asyncio.to_thread(_run, query.trends, hashtag, interval=interval, start=start, end=end)


async def map_points(
    hashtag: str | list[str], *, limit: int = 2000, start: dt.datetime | None = None, end: dt.datetime | None = None
):
    return await asyncio.to_thread(_run, query.map_points, hashtag, limit=limit, start=start, end=end)
