"""Whole-OSM (no-hashtag) stats over a recent window, capped at GLOBAL_MAX_DAYS."""

from datetime import UTC, datetime, timedelta
from typing import Any

from litestar import Controller, Router, get
from litestar.exceptions import HTTPException

from osmsg.query import GLOBAL_MAX_DAYS
from osmsg.query import LEADERBOARD_SORTS as _LEADERBOARD_SORTS

from .. import duck

_MAX = timedelta(days=GLOBAL_MAX_DAYS)
_WINDOWS = {
    k: v
    for k, v in {
        "1h": timedelta(hours=1),
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
    }.items()
    if v <= _MAX
}


def _resolve(window: str | None, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    if window is not None:
        if window not in _WINDOWS:
            raise HTTPException(status_code=400, detail=f"window must be one of {', '.join(_WINDOWS)}")
        now = datetime.now(UTC)
        return now - _WINDOWS[window], now
    if start is None or end is None:
        raise HTTPException(status_code=400, detail=f"provide window ({'|'.join(_WINDOWS)}) or both start and end")
    if start >= end:
        raise HTTPException(status_code=400, detail="start must be before end")
    if end - start > _MAX:
        raise HTTPException(status_code=400, detail=f"global window cannot exceed {GLOBAL_MAX_DAYS} days")
    if start < datetime.now(UTC) - _MAX - timedelta(days=2):
        raise HTTPException(status_code=400, detail=f"global stats cover only the last {GLOBAL_MAX_DAYS} days")
    return start, end


class GlobalController(Controller):
    path = "/global"

    @get("/summary")
    async def get_summary(
        self, window: str | None = None, start: datetime | None = None, end: datetime | None = None
    ) -> dict[str, Any]:
        s, e = _resolve(window, start, end)
        return await duck.global_summary(start=s, end=e)

    @get("/leaderboard")
    async def get_leaderboard(
        self,
        window: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        page: int = 1,
        page_size: int = 25,
        sort: str = "map_changes",
        order: str = "desc",
        q: str | None = None,
    ) -> dict[str, Any]:
        if sort not in _LEADERBOARD_SORTS:
            raise HTTPException(status_code=400, detail=f"sort must be one of {', '.join(_LEADERBOARD_SORTS)}")
        if order not in ("asc", "desc"):
            raise HTTPException(status_code=400, detail="order must be 'asc' or 'desc'")
        if page < 1:
            raise HTTPException(status_code=400, detail="page must be >= 1")
        s, e = _resolve(window, start, end)
        return await duck.global_leaderboard(
            start=s, end=e, page=page, page_size=page_size, sort=sort, order=order, q=q
        )

    @get("/editors")
    async def get_editors(
        self, window: str | None = None, start: datetime | None = None, end: datetime | None = None
    ) -> list[dict[str, Any]]:
        s, e = _resolve(window, start, end)
        return await duck.global_editors(start=s, end=e)

    @get("/tags")
    async def get_tags(
        self, window: str | None = None, start: datetime | None = None, end: datetime | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        s, e = _resolve(window, start, end)
        return await duck.global_tags(start=s, end=e, limit=limit)

    @get("/trending")
    async def get_trending(
        self, window: str | None = None, start: datetime | None = None, end: datetime | None = None, limit: int = 15
    ) -> list[dict[str, Any]]:
        s, e = _resolve(window, start, end)
        return await duck.global_trending(start=s, end=e, limit=limit)


global_router = Router(path="/api/v2", route_handlers=[GlobalController])
