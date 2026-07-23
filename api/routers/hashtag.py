"""All-time hashtag endpoints, served DuckDB-on-top: the published hashtag_changeset artifact
(history) combined with the recent tail derived on the fly from the base Postgres tables, through
osmsg's shared query surface. The path hashtag is comma-separated for multiple tags
(`/hashtag/hotosm,osmnepal/summary`), each matched as a prefix, the union deduped by changeset. An
optional half-open [start, end) window narrows any endpoint to a time range; omit both for all-time.
"""

from datetime import datetime
from typing import Any

from litestar import Controller, Router, get
from litestar.exceptions import HTTPException

from osmsg.query import TREND_INTERVALS as _TREND_INTERVALS

from .. import duck


def _hashtags(hashtag: str) -> list[str]:
    """Split the comma-separated path segment into one or more non-empty hashtags."""
    tags = [h.strip() for h in hashtag.split(",") if h.strip()]
    if not tags:
        raise HTTPException(status_code=400, detail="at least one hashtag is required")
    return tags


def _window(start: datetime | None, end: datetime | None) -> tuple[datetime | None, datetime | None]:
    """Validate an optional [start, end) window. Either bound may be omitted; an inverted range is a
    client error, not silently empty results."""
    if start is not None and end is not None and start >= end:
        raise HTTPException(status_code=400, detail="start must be before end")
    return start, end


class HashtagController(Controller):
    path = "/hashtag/{hashtag:str}"

    @get("/summary")
    async def get_summary(
        self, hashtag: str, start: datetime | None = None, end: datetime | None = None
    ) -> dict[str, Any]:
        start, end = _window(start, end)
        return await duck.summary(_hashtags(hashtag), start=start, end=end)

    @get("/leaderboard")
    async def get_leaderboard(
        self,
        hashtag: str,
        limit: int = 100,
        offset: int = 0,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        start, end = _window(start, end)
        return await duck.leaderboard(_hashtags(hashtag), limit=limit, offset=offset, start=start, end=end)

    @get("/tags")
    async def get_tags(
        self, hashtag: str, limit: int = 100, start: datetime | None = None, end: datetime | None = None
    ) -> list[dict[str, Any]]:
        start, end = _window(start, end)
        return await duck.tags(_hashtags(hashtag), limit=limit, start=start, end=end)

    @get("/editors")
    async def get_editors(
        self, hashtag: str, start: datetime | None = None, end: datetime | None = None
    ) -> list[dict[str, Any]]:
        start, end = _window(start, end)
        return await duck.editors(_hashtags(hashtag), start=start, end=end)

    @get("/trends")
    async def get_trends(
        self, hashtag: str, interval: str = "day", start: datetime | None = None, end: datetime | None = None
    ) -> list[dict[str, Any]]:
        if interval not in _TREND_INTERVALS:
            raise HTTPException(status_code=400, detail=f"interval must be one of {', '.join(_TREND_INTERVALS)}")
        start, end = _window(start, end)
        return await duck.trends(_hashtags(hashtag), interval=interval, start=start, end=end)

    @get("/map")
    async def get_map(
        self,
        hashtag: str,
        limit: int = 2000,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        """A GeoJSON FeatureCollection of changeset centroids (Point), for clustering and heatmaps."""
        start, end = _window(start, end)
        points = await duck.map_points(_hashtags(hashtag), limit=limit, start=start, end=end)
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                    "properties": {"changeset_id": p["changeset_id"], "uid": p["uid"]},
                }
                for p in points
            ],
        }


v2_router = Router(path="/api/v2", route_handlers=[HashtagController])
