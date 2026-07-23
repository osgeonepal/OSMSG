# Self-hosting osmsg

Runs osmsg continuously: Postgres store, REST API, replication worker, and Caddy for HTTPS plus the
static leaderboard.

## Stack

| Service | Image | Role |
| --- | --- | --- |
| `db` | `postgis/postgis:17-3.5-alpine` | Stats store |
| `api` | `ghcr.io/osgeonepal/osmsg-api:latest` | REST API |
| `worker` | `ghcr.io/osgeonepal/osmsg-worker:latest` | Replication worker |
| `caddy` | `caddy:2-alpine` | HTTPS proxy: API + leaderboard |

Frontend and API use two subdomains: `OSMSG_FRONTEND_DOMAIN` and `OSMSG_API_DOMAIN`.

## Disk

Keep growing data off the root disk. Attach a block volume at `/mnt/mnt`; the three data volumes bind
to it. Create the directories and swap before the first start:

```bash
sudo mkdir -p /mnt/mnt/osmsg/{pgdata,cache,data,maintain/work,maintain/out}
sudo fallocate -l 4G /mnt/mnt/swapfile && sudo chmod 600 /mnt/mnt/swapfile
sudo mkswap /mnt/mnt/swapfile && sudo swapon /mnt/mnt/swapfile
echo '/mnt/mnt/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## DNS

Point both subdomains at the server as `A` records. On Cloudflare, set them DNS-only (grey cloud) so
Caddy's Let's Encrypt challenge reaches the host.

```text
osmsg.<domain>      A   <server-ip>
api.osmsg.<domain>  A   <server-ip>
```

## Configure and deploy

```bash
cp infra/.env.example infra/.env && $EDITOR infra/.env   # set the two domains + ACME email
sudo mkdir -p /opt/osmsg/infra
sudo cp infra/docker-compose.yml infra/Caddyfile infra/osmsg.service infra/.env /opt/osmsg/infra/
sudo git clone https://github.com/osgeonepal/osmsg-leaderboard.git /opt/osmsg-leaderboard
sudo cp /opt/osmsg/infra/osmsg.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now osmsg
```

`OSMSG_EXTRA_ARGS` runs every tick. Do not put `--last`, `--days`, `--update`, or `--url` there: the
worker adds `--update` and auto-selects granularity from the gap (day to hour to minute). Pinning
`--url minute` over a large gap crawls tens of thousands of files and fills the disk.

## Seed history, then follow live

Seed the last published month into Postgres, then let the worker follow live. Use the worker's store
params so resume state lines up.

```bash
cd /opt/osmsg/infra && docker compose up -d db
docker compose run --rm --entrypoint osmsg worker \
    --insert --start 2026-05-01 --end 2026-06-01 \
    --name stats --output-dir /var/lib/osmsg --cache-dir /var/cache/osmsg \
    --format psql --psql-dsn postgresql://osmsg:osmsg@db:5432/osmsg --all
docker compose up -d
```

Manifest max month: `.../osmsg-history/resolve/main/manifest.json`.

## Timers

```bash
sudo cp infra/osmsg-cache-prune.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now osmsg-cache-prune.timer
```

Monthly HuggingFace dump (`maintain month` needs `uv` + source, so it runs host-side):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
sudo git clone https://github.com/osgeonepal/osmsg.git /opt/osmsg-maintain
cd /opt/osmsg-maintain && sudo uv sync --no-dev
sudo cp /opt/osmsg/infra/run-maintain.sh /opt/osmsg-maintain/
echo 'HF_TOKEN=<write-token>' | sudo tee /opt/osmsg-maintain/.env && sudo chmod 600 /opt/osmsg-maintain/.env
sudo cp infra/osmsg-maintain.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now osmsg-maintain.timer
```

`run-maintain.sh` keeps all scratch on the block volume. Publish a month by hand with
`sudo systemctl start osmsg-maintain.service` (previous month) or pass a month:
`sudo -E .../run-maintain.sh 2026-06`.

## Operate

```bash
journalctl -u osmsg -f                        # container logs
cd /opt/osmsg/infra && docker compose pull && docker compose up -d   # update images
```

Recover a full root disk (cache or images landed on root):

```bash
systemctl stop osmsg && cd /opt/osmsg/infra && docker compose down
docker volume rm infra_osmsg-cache infra_osmsg-data   # pgdata is never removed
docker image prune -af && docker builder prune -af
```

## API

All-time hashtag stats, combining the published history rollup with the live tail at query time (see
`docs/rollups.md`).

```text
GET /health
GET /api/v2/hashtag/{hashtag}/summary[?start=<ISO8601>&end=<ISO8601>]
GET /api/v2/hashtag/{hashtag}/leaderboard[?limit=N&offset=N&start=<ISO8601>&end=<ISO8601>]
GET /api/v2/hashtag/{hashtag}/tags[?limit=N&start=<ISO8601>&end=<ISO8601>]
GET /api/v2/hashtag/{hashtag}/editors[?start=<ISO8601>&end=<ISO8601>]
GET /api/v2/hashtag/{hashtag}/trends[?interval=day|week|month&start=<ISO8601>&end=<ISO8601>]
GET /api/v2/hashtag/{hashtag}/map[?limit=N&start=<ISO8601>&end=<ISO8601>]
GET /docs/swagger
```

`{hashtag}` is matched as a prefix (`hotosm` covers `#hotosm-project-1`, `#hotosm-fanclub`, …), deduped
by changeset so a changeset carrying two matching hashtags counts once. Pass several comma-separated
(`/hashtag/hotosm,osmnepal/summary`) to scope to the union of them; a changeset tagged with more than
one still counts once. `summary` returns totals (users, changesets, the element breakdown,
map_changes); `leaderboard` ranks contributors by map changes with their editors; `tags` is the
key/value breakdown; `editors` is the editor breakdown. Every endpoint accepts an optional half-open
`[start, end)` UTC window; omit both for all-time. The window intersects the history/live split, so a
range before the published frontier reads only the history rollup, a range after it reads only the live
tail, and a straddling range reads both. `trends` buckets by day, week, or month. `map` returns a
GeoJSON FeatureCollection of changeset centroids (Point) for clustering/heatmaps, up to `limit`; its
history side needs the `lon`/`lat` columns that the rollup gained with this feature, so an all-time map
requires a rollup rebuilt by `maintain` (the recent tail maps immediately from the base bbox).

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
