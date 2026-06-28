from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TagValueStats(BaseModel):
    c: int = 0
    m: int = 0
    len: float | None = None


class TagKeyStats(BaseModel):
    c: int = 0
    m: int = 0
    len: float | None = None


class PaginationMeta(BaseModel):
    limit: int
    offset: int
    returned: int
    has_next: bool
    has_previous: bool
    next_offset: int | None
    previous_offset: int | None


class UserStat(BaseModel):
    uid: int
    name: str
    changesets: int
    nodes_create: int
    nodes_modify: int
    nodes_delete: int
    ways_create: int
    ways_modify: int
    ways_delete: int
    rels_create: int
    rels_modify: int
    rels_delete: int
    poi_create: int
    poi_modify: int
    map_changes: int
    rank: int
    hashtags: list[str] = Field(default_factory=list)
    tag_stats: dict[str, TagKeyStats | dict[str, TagValueStats]] | None = None


class UserStatsResponse(BaseModel):
    count: int
    start: datetime | None
    end: datetime | None
    hashtag: list[str] | None
    tags: bool
    tag_mode: Literal["none", "keys", "all"]
    limit: int
    offset: int
    pagination: PaginationMeta
    users: list[UserStat]


class MapFeatureCollection(BaseModel):
    type: str = "FeatureCollection"
    count: int
    limit: int
    offset: int
    pagination: PaginationMeta
    features: list[dict[str, Any]]


class HashtagStat(BaseModel):
    hashtag: str
    changesets: int
    users: int
    map_changes: int
    rank: int


class HashtagTrend(BaseModel):
    period_start: datetime
    hashtag: str
    changesets: int
    users: int
    map_changes: int


class HashtagTrendsResponse(BaseModel):
    interval: str
    trends: list[HashtagTrend]


class HashtagStatsResponse(BaseModel):
    count: int
    start: datetime
    end: datetime
    hashtag: list[str] | None
    interval: str
    limit: int
    offset: int
    pagination: PaginationMeta
    hashtags: list[HashtagStat]
    trends: list[HashtagTrend]


class EditorStat(BaseModel):
    editor: str
    changesets: int
    users: int
    map_changes: int
    rank: int


class EditorStatsResponse(BaseModel):
    count: int
    start: datetime | None
    end: datetime | None
    include_version: bool
    limit: int
    offset: int
    pagination: PaginationMeta
    editors: list[EditorStat]


class HealthResponse(BaseModel):
    status: str
    last_seq: int | None
    last_ts: datetime | None
    updated_at: datetime | None
