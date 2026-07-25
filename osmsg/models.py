"""Pydantic models for the pipeline. In-handler stats use ergonomic helpers, then flatten to plain
columns (`to_row()`) for a schema portable across DuckDB, Parquet, and Postgres."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Action(StrEnum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class User(BaseModel):
    uid: int
    username: str

    def to_row(self) -> tuple[int, str]:
        return (self.uid, self.username)


class Changeset(BaseModel):
    changeset_id: int
    uid: int
    created_at: datetime | None = None
    hashtags: list[str] = Field(default_factory=list)
    editor: str | None = None
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="(min_lon, min_lat, max_lon, max_lat)"
    )

    def to_row(self) -> tuple:
        if self.bbox is None:
            min_lon = min_lat = max_lon = max_lat = None
        else:
            min_lon, min_lat, max_lon, max_lat = self.bbox
        return (
            self.changeset_id,
            self.uid,
            self.created_at,
            self.hashtags or None,
            self.editor,
            min_lon,
            min_lat,
            max_lon,
            max_lat,
        )


class TagValueStat(BaseModel):
    c: int = 0
    m: int = 0
    len: float | None = None

    def add(self, action: Action) -> None:
        if action is Action.CREATE:
            self.c += 1
        elif action is Action.MODIFY:
            self.m += 1

    def add_length(self, meters: float) -> None:
        self.len = (self.len or 0.0) + meters


class ElementStat(BaseModel):
    """Create/modify/delete counts for a single OSM element type."""

    c: int = 0
    m: int = 0
    d: int = 0

    @property
    def total(self) -> int:
        return self.c + self.m + self.d

    def add(self, action: Action) -> None:
        if action is Action.CREATE:
            self.c += 1
        elif action is Action.MODIFY:
            self.m += 1
        elif action is Action.DELETE:
            self.d += 1


class ChangesetStats(BaseModel):
    """Per-changeset accumulator. Flattens to a row of count columns plus a native `tags` list."""

    changeset_id: int
    uid: int
    seq_id: int

    nodes: ElementStat = Field(default_factory=ElementStat)
    ways: ElementStat = Field(default_factory=ElementStat)
    rels: ElementStat = Field(default_factory=ElementStat)

    poi_created: int = 0
    poi_modified: int = 0

    tag_stats: dict[str, dict[str, TagValueStat]] = Field(default_factory=dict)

    @property
    def map_changes(self) -> int:
        return self.nodes.total + self.ways.total + self.rels.total

    def tags_list(self) -> list[dict[str, Any]]:
        """Flat native tag rows ({k,v,c,m,len_m}) for the shard's LIST<STRUCT> column, so ingest is a copy."""
        return [
            {"k": key, "v": value, "c": tv.c, "m": tv.m, "len_m": round(tv.len, 2) if tv.len is not None else None}
            for key, by_value in self.tag_stats.items()
            for value, tv in by_value.items()
        ]

    def to_row(self) -> tuple:
        return (
            self.changeset_id,
            self.seq_id,
            self.uid,
            self.nodes.c,
            self.nodes.m,
            self.nodes.d,
            self.ways.c,
            self.ways.m,
            self.ways.d,
            self.rels.c,
            self.rels.m,
            self.rels.d,
            self.poi_created,
            self.poi_modified,
            self.tags_list(),
        )
