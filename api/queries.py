from typing import Any

from .db import get_pool


async def fetch_state() -> dict[str, Any] | None:
    # last_ts/last_seq come from the worst-lagging source (slowest source bounds real freshness);
    # updated_at is the most recent heartbeat across all sources (any tick proves the worker is alive).
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT last_seq, last_ts, (SELECT MAX(updated_at) FROM state) AS updated_at
            FROM state
            ORDER BY last_ts ASC
            LIMIT 1
            """
        )
    if row is None:
        return None
    return dict(row)
