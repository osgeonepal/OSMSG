from datetime import datetime

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    last_seq: int | None
    last_ts: datetime | None
    updated_at: datetime | None
