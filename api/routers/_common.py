"""Shared helpers for the v2 routers."""

from datetime import UTC, datetime


def to_utc(value: datetime | None) -> datetime | None:
    """Normalize a query-parameter datetime to UTC. Litestar parses an offset-less ISO string as naive;
    assume UTC for it so a window is not shifted by the server's local offset or compared against an
    aware `now()`."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
