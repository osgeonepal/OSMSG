import argparse
import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from api.queries import _editor_stats_sql, _hashtag_stats_sql, _hashtag_trends_sql


def parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def normalize_hashtags(values: list[str]) -> list[str]:
    return ["#" + value.strip().lstrip("#") for value in values if value.strip()]


async def explain(conn: asyncpg.Connection, name: str, sql: str, params: list[Any]) -> None:
    plan_rows = await conn.fetch(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}", *params)
    print(f"\n=== {name} ===")
    for row in plan_rows:
        print(row["QUERY PLAN"])


async def table_counts(conn: asyncpg.Connection) -> None:
    print("=== table counts ===")
    for table in ("users", "changesets", "changeset_stats"):
        count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
        print(f"{table}: {count:,}")
    row = await conn.fetchrow(
        "SELECT MIN(created_at) AS min_created_at, MAX(created_at) AS max_created_at FROM changesets"
    )
    print(f"changesets.created_at: {row['min_created_at']} -> {row['max_created_at']}")


async def create_synthetic_tables(conn: asyncpg.Connection, rows: int) -> None:
    await conn.execute(
        """
        CREATE TEMP TABLE users (
            uid      BIGINT PRIMARY KEY,
            username TEXT NOT NULL
        ) ON COMMIT PRESERVE ROWS
        """
    )
    await conn.execute(
        """
        CREATE TEMP TABLE changesets (
            changeset_id BIGINT PRIMARY KEY,
            uid          BIGINT NOT NULL,
            created_at   TIMESTAMPTZ,
            hashtags     TEXT[],
            editor       TEXT
        ) ON COMMIT PRESERVE ROWS
        """
    )
    await conn.execute(
        """
        CREATE TEMP TABLE changeset_stats (
            changeset_id   BIGINT NOT NULL,
            seq_id         BIGINT NOT NULL,
            uid            BIGINT NOT NULL,
            nodes_created  INTEGER DEFAULT 0,
            nodes_modified INTEGER DEFAULT 0,
            nodes_deleted  INTEGER DEFAULT 0,
            ways_created   INTEGER DEFAULT 0,
            ways_modified  INTEGER DEFAULT 0,
            ways_deleted   INTEGER DEFAULT 0,
            rels_created   INTEGER DEFAULT 0,
            rels_modified  INTEGER DEFAULT 0,
            rels_deleted   INTEGER DEFAULT 0,
            poi_created    INTEGER DEFAULT 0,
            poi_modified   INTEGER DEFAULT 0,
            tag_stats      JSONB,
            PRIMARY KEY (seq_id, changeset_id)
        ) ON COMMIT PRESERVE ROWS
        """
    )
    await conn.execute(
        """
        INSERT INTO users (uid, username)
        SELECT uid, 'user_' || uid::text
        FROM generate_series(1, LEAST($1::bigint, 100000::bigint)) AS uid
        """,
        rows,
    )
    await conn.execute(
        """
        INSERT INTO changesets (changeset_id, uid, created_at, hashtags, editor)
        SELECT
            id,
            (id % LEAST($1::bigint, 100000::bigint)) + 1,
            TIMESTAMPTZ '2026-05-01T00:00:00Z' + ((id % 43200) * INTERVAL '1 minute'),
            CASE
                WHEN id % 10 = 0 THEN ARRAY['#maproulette', '#tomtom']
                WHEN id % 7 = 0 THEN ARRAY['#hotosm']
                WHEN id % 5 = 0 THEN ARRAY['#osmnepal', '#buildings']
                ELSE ARRAY[]::TEXT[]
            END,
            CASE
                WHEN id % 4 = 0 THEN 'iD 2.34.0'
                WHEN id % 4 = 1 THEN 'JOSM'
                WHEN id % 4 = 2 THEN 'StreetComplete'
                ELSE NULL
            END
        FROM generate_series(1, $1::bigint) AS id
        """,
        rows,
    )
    await conn.execute(
        """
        INSERT INTO changeset_stats (
            changeset_id, seq_id, uid, nodes_created, nodes_modified, nodes_deleted,
            ways_created, ways_modified, ways_deleted, rels_created, rels_modified, rels_deleted,
            poi_created, poi_modified
        )
        SELECT
            id,
            id,
            (id % LEAST($1::bigint, 100000::bigint)) + 1,
            (id % 97)::integer,
            (id % 13)::integer,
            (id % 3)::integer,
            (id % 11)::integer,
            (id % 5)::integer,
            (id % 2)::integer,
            (id % 7)::integer,
            (id % 3)::integer,
            (id % 2)::integer,
            (id % 17)::integer,
            (id % 5)::integer
        FROM generate_series(1, $1::bigint) AS id
        """,
        rows,
    )
    await ensure_indexes(conn)
    await conn.execute("ANALYZE users")
    await conn.execute("ANALYZE changesets")
    await conn.execute("ANALYZE changeset_stats")


async def ensure_indexes(conn: asyncpg.Connection) -> None:
    statements = [
        "CREATE INDEX IF NOT EXISTS idx_changesets_created_at ON changesets(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_changesets_hashtags ON changesets USING GIN (hashtags)",
        "CREATE INDEX IF NOT EXISTS idx_changesets_editor ON changesets(editor)",
        "CREATE INDEX IF NOT EXISTS idx_changeset_stats_changeset_id ON changeset_stats(changeset_id)",
    ]
    for statement in statements:
        await conn.execute(statement)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run EXPLAIN ANALYZE for API analytics queries.")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"), help="PostgreSQL connection URL.")
    parser.add_argument("--start", type=parse_dt, help="Inclusive UTC lower bound, e.g. 2026-05-01T00:00:00Z.")
    parser.add_argument("--end", type=parse_dt, help="Exclusive UTC upper bound, e.g. 2026-05-02T00:00:00Z.")
    parser.add_argument("--days", type=int, default=30, help="Window size when start/end are omitted.")
    parser.add_argument("--hashtag", action="append", default=[], help="Optional hashtag filter. Repeatable.")
    parser.add_argument("--interval", choices=("day", "week", "month"), default="day", help="Trend bucket.")
    parser.add_argument("--limit", type=int, default=100, help="Limit used by leaderboard queries.")
    parser.add_argument("--offset", type=int, default=0, help="Offset used by leaderboard queries.")
    parser.add_argument("--ensure-indexes", action="store_true", help="Create analytics indexes before measuring.")
    parser.add_argument("--synthetic-rows", type=int, help="Use temporary generated tables with this many rows.")
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL is required. Set it in the environment or pass --database-url.")

    end = args.end or datetime.now(tz=UTC)
    start = args.start or (end - timedelta(days=args.days))
    if start >= end:
        raise SystemExit("start must be before end")

    hashtags = normalize_hashtags(args.hashtag)

    conn = await asyncpg.connect(args.database_url)
    try:
        if args.synthetic_rows:
            print(f"Creating temporary synthetic dataset with {args.synthetic_rows:,} rows...")
            await create_synthetic_tables(conn, args.synthetic_rows)
        if args.ensure_indexes:
            await ensure_indexes(conn)
        await table_counts(conn)

        hashtag_stats_params: list[Any] = [start, end]
        if hashtags:
            hashtag_stats_params.append(hashtags)
        hashtag_stats_params.extend([args.limit, args.offset])
        await explain(
            conn,
            "hashtag stats",
            _hashtag_stats_sql(filter_dates=True, filter_hashtags=bool(hashtags)),
            hashtag_stats_params,
        )

        trend_params: list[Any] = [start, end]
        if hashtags:
            trend_params.append(hashtags)
        trend_params.extend([args.interval, args.limit, args.offset])
        await explain(
            conn,
            "hashtag trends",
            _hashtag_trends_sql(filter_hashtags=bool(hashtags)),
            trend_params,
        )

        await explain(
            conn,
            "editor stats",
            _editor_stats_sql(filter_dates=True, include_version=False),
            [start, end, args.limit, args.offset],
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
