from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast

from litestar import Controller, Router, get
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from ..pagination import PaginationParams, paginate_items
from ..queries import fetch_editor_stats, fetch_hashtag_stats, fetch_hashtag_trends, fetch_map_changes, fetch_user_stats
from ..schemas import (
    EditorStat,
    EditorStatsResponse,
    HashtagStat,
    HashtagStatsResponse,
    HashtagTrend,
    MapFeatureCollection,
    UserStat,
    UserStatsResponse,
)

TREND_INTERVALS = {"day", "week", "month"}
TAG_MODES = {"keys", "all"}


def normalize_hashtags(hashtag: list[str] | None) -> list[str] | None:
    if not hashtag:
        return None

    normalized: list[str] = []
    seen: set[str] = set()
    for value in hashtag:
        cleaned = value.strip()
        if not cleaned:
            continue
        cleaned = "#" + cleaned.lstrip("#")
        key = cleaned.lower()
        if key not in seen:
            normalized.append(cleaned)
            seen.add(key)
    return normalized or None


def resolve_optional_window(start: datetime | None, end: datetime | None) -> tuple[datetime | None, datetime | None]:
    start = start or (datetime.min.replace(tzinfo=UTC) if end else None)
    end = end or (datetime.now(tz=UTC) if start else None)
    if start and end and start >= end:
        raise HTTPException(status_code=400, detail="start must be before end")
    return start, end


def resolve_required_window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    end = end or datetime.now(tz=UTC)
    start = start or (end - timedelta(days=30))
    if start >= end:
        raise HTTPException(status_code=400, detail="start must be before end")
    return start, end


class StatsController(Controller):
    path = "/stats"

    @get()
    async def get_user_stats(
        self,
        start: Annotated[
            datetime | None, Parameter(description="Inclusive UTC lower bound (ISO 8601). If omitted, no lower bound.")
        ] = None,
        end: Annotated[
            datetime | None,
            Parameter(description="Exclusive UTC upper bound (ISO 8601). Defaults to now if start is set."),
        ] = None,
        hashtag: Annotated[
            list[str] | None, Parameter(description="Filter to changesets carrying any of these hashtags. Repeatable.")
        ] = None,
        tags: Annotated[bool, Parameter(description="Include per-user tag_stats breakdown in the response.")] = True,
        tag_mode: Annotated[
            str,
            Parameter(description="Tag aggregation: keys (default compact totals) or all (key/value details)."),
        ] = "keys",
        limit: Annotated[int, Parameter(ge=1, le=1000, description="Page size (1 to 1000).")] = 100,
        offset: Annotated[int, Parameter(ge=0, description="Page offset.")] = 0,
    ) -> UserStatsResponse:
        start, end = resolve_optional_window(start, end)
        normalized_hashtag = normalize_hashtags(hashtag)
        if tag_mode not in TAG_MODES:
            raise HTTPException(status_code=400, detail="tag_mode must be one of: keys, all")
        resolved_tag_mode = "none" if not tags else cast(Literal["keys", "all"], tag_mode)
        page_params = PaginationParams(limit=limit, offset=offset)
        rows = await fetch_user_stats(
            start=start,
            end=end,
            hashtag=normalized_hashtag,
            tag_mode=resolved_tag_mode,
            limit=page_params.query_limit,
            offset=page_params.offset,
        )
        page = paginate_items([UserStat(**row) for row in rows], page_params)
        return UserStatsResponse(
            count=len(page.items),
            start=start,
            end=end,
            hashtag=normalized_hashtag,
            tags=resolved_tag_mode != "none",
            tag_mode=resolved_tag_mode,
            limit=limit,
            offset=offset,
            pagination=page,
            users=page.items,
        )


class HashtagStatsController(Controller):
    path = "/hashtag-stats"

    @get()
    async def get_hashtag_stats(
        self,
        start: Annotated[
            datetime | None,
            Parameter(description="Inclusive UTC lower bound (ISO 8601). Defaults to 30 days before end."),
        ] = None,
        end: Annotated[
            datetime | None,
            Parameter(description="Exclusive UTC upper bound (ISO 8601). Defaults to now."),
        ] = None,
        hashtag: Annotated[
            list[str] | None, Parameter(description="Optional hashtags to limit the leaderboard to. Repeatable.")
        ] = None,
        interval: Annotated[str, Parameter(description="Trend bucket: day, week, or month.")] = "day",
        limit: Annotated[int, Parameter(ge=1, le=1000, description="Page size (1-1000).")] = 100,
        offset: Annotated[int, Parameter(ge=0, description="Page offset.")] = 0,
    ) -> HashtagStatsResponse:
        if interval not in TREND_INTERVALS:
            raise HTTPException(status_code=400, detail="interval must be one of: day, week, month")

        start, end = resolve_required_window(start, end)
        normalized_hashtag = normalize_hashtags(hashtag)
        page_params = PaginationParams(limit=limit, offset=offset)
        hashtag_rows = await fetch_hashtag_stats(
            start=start,
            end=end,
            hashtag=normalized_hashtag,
            limit=page_params.query_limit,
            offset=page_params.offset,
        )
        trend_rows = await fetch_hashtag_trends(
            start=start,
            end=end,
            interval=interval,
            hashtag=normalized_hashtag,
            limit=page_params.query_limit,
            offset=page_params.offset,
        )
        hashtag_page = paginate_items([HashtagStat(**row) for row in hashtag_rows], page_params)
        trend_page = paginate_items([HashtagTrend(**row) for row in trend_rows], page_params)
        return HashtagStatsResponse(
            count=len(hashtag_page.items),
            start=start,
            end=end,
            hashtag=normalized_hashtag,
            interval=interval,
            limit=limit,
            offset=offset,
            pagination=hashtag_page,
            hashtags=hashtag_page.items,
            trends=trend_page.items,
        )


class EditorStatsController(Controller):
    path = "/editor-stats"

    @get()
    async def get_editor_stats(
        self,
        start: Annotated[
            datetime | None, Parameter(description="Inclusive UTC lower bound (ISO 8601). If omitted, no lower bound.")
        ] = None,
        end: Annotated[
            datetime | None,
            Parameter(description="Exclusive UTC upper bound (ISO 8601). Defaults to now if start is set."),
        ] = None,
        include_version: Annotated[
            bool,
            Parameter(description="Include editor versions instead of grouping results by editor family."),
        ] = False,
        limit: Annotated[int, Parameter(ge=1, le=1000, description="Page size (1-1000).")] = 100,
        offset: Annotated[int, Parameter(ge=0, description="Page offset.")] = 0,
    ) -> EditorStatsResponse:
        start, end = resolve_optional_window(start, end)
        page_params = PaginationParams(limit=limit, offset=offset)
        rows = await fetch_editor_stats(
            start=start,
            end=end,
            include_version=include_version,
            limit=page_params.query_limit,
            offset=page_params.offset,
        )
        page = paginate_items([EditorStat(**row) for row in rows], page_params)
        return EditorStatsResponse(
            count=len(page.items),
            start=start,
            end=end,
            include_version=include_version,
            limit=limit,
            offset=offset,
            pagination=page,
            editors=page.items,
        )


class MapController(Controller):
    path = "/map"

    @get()
    async def get_map(
        self,
        start: Annotated[
            datetime | None, Parameter(description="Inclusive UTC lower bound (ISO 8601). If omitted, no lower bound.")
        ] = None,
        end: Annotated[
            datetime | None,
            Parameter(description="Exclusive UTC upper bound (ISO 8601). Defaults to now if start is set."),
        ] = None,
        hashtag: Annotated[
            list[str] | None, Parameter(description="Filter to changesets carrying any of these hashtags. Repeatable.")
        ] = None,
        limit: Annotated[int, Parameter(ge=1, le=1000, description="Page size (1-1000).")] = 500,
        offset: Annotated[int, Parameter(ge=0, description="Page offset.")] = 0,
    ) -> MapFeatureCollection:
        start, end = resolve_optional_window(start, end)
        normalized_hashtag = normalize_hashtags(hashtag)
        page_params = PaginationParams(limit=limit, offset=offset)
        feature_collection = await fetch_map_changes(
            start=start,
            end=end,
            hashtag=normalized_hashtag,
            limit=page_params.query_limit,
            offset=page_params.offset,
        )
        page = paginate_items(feature_collection["features"], page_params)
        return MapFeatureCollection(
            count=len(page.items),
            limit=limit,
            offset=offset,
            pagination=page,
            features=page.items,
        )


v1_router = Router(
    path="/api/v1",
    route_handlers=[StatsController, HashtagStatsController, EditorStatsController, MapController],
)
