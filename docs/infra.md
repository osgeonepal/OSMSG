# Self-hosting osmsg

This guide covers running osmsg continuously on a server.

## Two compose files

| File | Purpose | Images |
| --- | --- | --- |
| `docker-compose.yml` | Local development | Built from source |
| `infra/docker-compose.yml` | Production / server | Pulled from GHCR |

The production compose adds Caddy for HTTPS termination and pulls pre-built images

## Local development

```bash
docker compose up -d
curl 'http://localhost:8000/health'
```

The API is available on port 8000 directly. No config needed: defaults to planet replication,
`*/2 * * * *` schedule, bootstrap from last hour.

## Production deployment

### Stack

| Service | Image | Role |
| --- | --- | --- |
| `db` | `postgres:17-alpine` | Persistent stats store |
| `api` | `ghcr.io/osgeonepal/osmsg-api:latest` | Litestar REST API |
| `worker` | `ghcr.io/osgeonepal/osmsg-worker:latest` | osmsg cron worker |
| `caddy` | `caddy:2-alpine` | HTTPS reverse proxy |

### Configuration

Copy `infra/.env.example` and edit:

```bash
cp infra/.env.example infra/.env
$EDITOR infra/.env
```

| Variable | Default | Notes |
| --- | --- | --- |
| `OSMSG_DOMAIN` | `localhost` | Your domain, enables automatic HTTPS via Caddy |
| `OSMSG_SCHEDULE` | `*/2 * * * *` | supercronic cron expression |
| `OSMSG_BOOTSTRAP` | `hour` | First-run window: `hour`/`day`/`week`/`month`/`year` |
| `OSMSG_BOOTSTRAP_DAYS` | _unset_ | Exact day count for first run (alternative to `OSMSG_BOOTSTRAP`) |
| `OSM_USERNAME` | _unset_ | OSM account username (required for Geofabrik country replication) |
| `OSM_PASSWORD` | _unset_ | OSM account password (required for Geofabrik country replication) |
| `OSMSG_EXTRA_ARGS` | _see example_ | osmsg args applied on every tick (country, format, tags, boundary, etc.) |

`OSMSG_EXTRA_ARGS` runs on every tick. Do not put `--last`, `--days`, or `--update` here;
tick adds those automatically based on whether state exists.

Geofabrik sub-daily replication uses your OSM credentials directly, no browser opt-in required.

### Start

```bash
cd infra
docker compose up -d
curl 'http://localhost/health'
```

Set `OSMSG_DOMAIN` to your server's hostname for automatic HTTPS.

### Update to latest images

```bash
cd infra
docker compose pull && docker compose up -d
```

## Run as a systemd service

Only the `infra/` directory needs to be on the server; no source code or build tools required.

**1. Place files:**

```bash
mkdir -p /opt/osmsg/infra
cp infra/docker-compose.yml infra/Caddyfile infra/osmsg.service /opt/osmsg/infra/
cp infra/.env.example /opt/osmsg/infra/.env
$EDITOR /opt/osmsg/infra/.env

# The pgdata Docker volume binds to /mnt, create the directory first
mkdir -p /mnt/osmsg/pgdata
```

**2. Install and enable:**

```bash
cp /opt/osmsg/infra/osmsg.service /etc/systemd/system/osmsg.service
systemctl daemon-reload
systemctl enable --now osmsg
```

**Useful commands:**

```bash
systemctl status osmsg
journalctl -u osmsg -f       # follow logs from all containers
systemctl restart osmsg      # pick up .env changes
systemctl stop osmsg         # graceful shutdown
```

## Populate all-time stats (backfill)

Run the worker once with a date range before starting the continuous service.
The worker detects existing state and resumes with `--update` automatically on next ticks.

**Nepal stats since 2012:**

```bash
cd infra
docker compose up -d db

docker compose run --rm worker python -m osmsg \
    --name nepal \
    --country nepal \
    --start "2012-09-12" \
    --end "2026-01-01" \
    --format psql \
    --psql-dsn "postgresql://osmsg:osmsg@db:5432/osmsg"

docker compose up -d
```

**Last 90 days then keep refreshing:**

```bash
# Set OSMSG_EXTRA_ARGS with --days 90 for first run, then start normally
OSMSG_EXTRA_ARGS="--name stats --output-dir /var/lib/osmsg --cache-dir /var/cache/osmsg --url minute --days 90 --format psql --psql-dsn postgresql://osmsg:osmsg@db:5432/osmsg" \
  docker compose up -d
```

## API endpoints

```text
GET /
GET /health
GET /api/v1/stats?start=<ISO8601>&end=<ISO8601>[&hashtag=<tag>][&tags=true|false][&tag_mode=keys|all][&limit=N][&offset=N]
GET /api/v1/hashtag-stats?start=<ISO8601>&end=<ISO8601>[&hashtag=<tag>][&interval=day|week|month][&limit=N][&offset=N]
GET /api/v1/editor-stats?start=<ISO8601>&end=<ISO8601>[&include_version=true|false][&limit=N][&offset=N]
GET /api/v1/map?start=<ISO8601>&end=<ISO8601>[&hashtag=<tag>][&limit=N][&offset=N]
GET /docs/swagger
```

`tags=true&tag_mode=keys` (the default) returns compact per-key totals. Use
`tag_mode=all` to opt in to the full key/value breakdown, or `tags=false` to skip
JSONB expansion for cheaper responses. Editor stats group versions into editor
families by default; use `include_version=true` for full version strings. Collection
responses include a `pagination` object with next/previous offset metadata. Map
features use centroid Point geometry only, ready for clustering and heatmaps.

## Run the API standalone (without compose)

```bash
uv run osmsg --last day --format psql --psql-dsn "$DATABASE_URL" --name api_last_day
uv run --group api litestar --app api.app:app run --host 0.0.0.0 --port 8000
```

## Volumes

| Volume | Contents |
| --- | --- |
| `pgdata` | Postgres data |
| `osmsg-data` | DuckDB state files + parquet output |
| `osmsg-cache` | Downloaded replication diff cache |
| `caddy-data` | TLS certificates |
