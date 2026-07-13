from typing import TypeVar

from litestar.pagination import OffsetPagination
from pydantic import BaseModel, Field

RowT = TypeVar("RowT")


class PaginationParams(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)

    @property
    def query_limit(self) -> int:
        return self.limit + 1


def paginate_items(rows: list[RowT], params: PaginationParams) -> OffsetPagination[RowT]:
    has_next = len(rows) > params.limit
    items = rows[: params.limit]
    total = params.offset + len(items) + int(has_next)
    return OffsetPagination(items=items, limit=params.limit, offset=params.offset, total=total)
