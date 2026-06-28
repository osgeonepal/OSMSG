# osmsg

[![CI](https://github.com/osgeonepal/osmsg/actions/workflows/ci.yml/badge.svg)](https://github.com/osgeonepal/osmsg/actions/workflows/ci.yml)
[![Docker](https://github.com/osgeonepal/osmsg/actions/workflows/docker.yml/badge.svg)](https://github.com/osgeonepal/osmsg/actions/workflows/docker.yml)
[![PyPI](https://img.shields.io/pypi/v/osmsg.svg)](https://pypi.org/project/osmsg/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Container](https://img.shields.io/badge/ghcr.io-osgeonepal%2Fosmsg-2496ED?logo=docker)](https://github.com/osgeonepal/osmsg/pkgs/container/osmsg)

**OpenStreetMap Stats Generator.** A tiny CLI (and Python library) that turns OSM history into per-user counts
of nodes, ways, and relations created, modified, or deleted, written to parquet, csv, json, markdown, or Postgres.

A Project of [OSGeo Nepal](https://osgeonepal.org).

## What does it do?

- Per-user create/modify/delete counts over any time window.
- Tag and hashtag breakdowns (e.g. `building`, `#hotosm`).
- Country and custom-boundary filters via Geofabrik.
- Cron-friendly resume with `--update`.
- One-command setup: `osmsg --insert` loads all history into your store, `osmsg --update` keeps it current.
- Outputs you can query: parquet, csv, json, markdown, DuckDB, Postgres.
- Cloud-native history: months covered by a published parquet dataset are read remotely.

## Install

Pick the one that fits how you work.

```bash
uvx --from osmsg osmsg --last hour       # zero-install, one-shot run
pip install osmsg                        # into your project
uv tool install osmsg                    # standalone CLI
docker run --rm -v "$PWD:/work" -w /work ghcr.io/osgeonepal/osmsg:latest --last hour
```

`uvx` can run osmsg in a throwaway environment , no install, no virtualenv to manage. Works
with any flag combination, e.g. `uvx --from osmsg osmsg --last hour --tags building --summary -f parquet -f markdown`.

More ways to install:

```bash
conda install -c conda-forge osmsg                 # conda / mamba
brew install osgeonepal/tap/osmsg          # macOS / Linux (Homebrew tap)
```

On Windows, download `osmsg.exe` from the [latest release](https://github.com/osgeonepal/osmsg/releases)
and double-click it to open the desktop app. Pick a Quick range (last hour, day, week, month, year, or
all time) or type your own dates, set the options, click Compute, and open the output folder. The CLI
below is for macOS, Linux, and pip/uv users.

## Quick start

```bash
osmsg --last hour                        # planet, last hour
osmsg --last day --tags building         # last day with a tag breakdown
osmsg --hashtags hotosm --last day       # only changesets tagged #hotosm
```

That's it. A `stats.duckdb` and a `stats.parquet` show up in your current folder.

## Set up a full history store

Two commands give you a complete, self-updating store. The first loads all of OSM history from the
published dataset and records where to resume; the second catches up to now and runs on a schedule.

```bash
osmsg --insert            # load all history into stats.duckdb, then exit
osmsg --update            # catch up to now (repeat on cron)
```

`osmsg` clears the multi-week backlog on day diffs, then refines to finer diffs as the store stays
current. For near-real-time, run `osmsg --update --url minute`.

Pick your store with one flag. DuckDB is the default (`stats.duckdb`); add a DSN for Postgres:

```bash
osmsg --insert --psql-dsn "postgresql://user:pass@localhost/osmsg"
osmsg --update --psql-dsn "postgresql://user:pass@localhost/osmsg"
```

Load only a slice with `--start/--end`; `--update` then continues from the end of that slice:

```bash
osmsg --insert --start 2020-01-01 --end 2023-01-01
```

Already have the planet files? Insert from them directly:

```bash
osmsg --insert --osh-file history-latest.osh.pbf --changeset-file changesets-latest.osm.bz2
```

## Tutorials

### 1. Stats for a country

```bash
osmsg --country nepal --last day
```

`--country` resolves through Geofabrik and needs an OSM account. Set `OSM_USERNAME` and `OSM_PASSWORD`
in your shell or a `.env` file:

```bash
export OSM_USERNAME=you
export OSM_PASSWORD=secret
```

### 2. A custom date range with summaries

```bash
osmsg --start "2026-04-01" --end "2026-04-08" \
      --tags building --tags highway --summary
```

`--summary` adds a daily rollup file alongside the per-changeset stats.

### 3. Run on a schedule

```bash
osmsg --country nepal --update           # picks up where the last run stopped
```

Drop that into cron or a GitHub Actions schedule. State is stored inside the DuckDB file, so reruns are safe.

### 4. Query the output

```bash
duckdb stats.duckdb -c "SELECT username, SUM(nodes_created) AS n
                        FROM users JOIN changeset_stats USING (uid)
                        GROUP BY username ORDER BY n DESC LIMIT 10"
```

Same schema in DuckDB and Postgres: `users`, `changesets`, `changeset_stats`, `state`.

### 5. Run the API

Push stats into Postgres, then start the Litestar API:

```bash
osmsg --last day --format psql --psql-dsn "postgresql://user:pass@localhost/osmsg"
litestar --app api.app:app run --host 0.0.0.0 --port 8000
```

```text
GET /health
GET /api/v1/user-stats?start=2026-05-01T00:00:00Z&end=2026-05-02T00:00:00Z
GET /docs
```

For self-hosting with Docker Compose and systemd, see [docs/infra.md](./docs/infra.md).

### 6. Use it as a library

```python
from datetime import datetime, UTC
from osmsg import RunConfig, run

result = run(RunConfig(
    name="nepal",
    countries=["nepal"],
    start_date=datetime(2026, 4, 25, tzinfo=UTC),
    end_date=datetime(2026, 4, 26, tzinfo=UTC),
))
print(result["files"]["parquet"])
```

Same pipeline as the CLI.

### 7. Long flag lists? Use a config


```bash
osmsg --config nepal.yaml
```

Each option is a YAML key written with its underscore name: `output_dir`, `history_url`, `all_stats`,
`formats`, `psql_dsn`, and so on (not the dashed flag). See [docs/Manual.md](./docs/Manual.md).

## Output formats

Every run writes `stats.duckdb` (or `<--name>.duckdb`) plus the formats you ask for via
`-f parquet|csv|json|markdown|psql`. Parquet is the default. Open it with duckdb, polars, pandas, anything.

Rerunning the same query with a different `-f` re-exports from the existing `<name>.duckdb` instead of
refetching, so adding a format is instant. Pass `--overwrite` to force a fresh recompute.

## Configuration

Every meaningful flag has a matching `OSMSG_*` env var so the CLI, a `.env` file, and a
docker-compose `environment:` block all reach the same setting. CLI flag wins over env var.

| CLI flag | Env var | Default | Notes |
| --- | --- | --- | --- |
| `--name` | `OSMSG_NAME` | `stats` | Output basename; sets `<name>.duckdb`. |
| `--country` | `OSMSG_COUNTRY` | unset | Geofabrik region id(s). Comma-separated when set via env. |
| `--boundary` | `OSMSG_BOUNDARY` | unset | GeoJSON path or inline GeoJSON. |
| `--url` | `OSMSG_URL` | `minute` | `minute`/`hour`/`day` shortcut or full URL. Comma-separated when set via env. |
| `--workers` | `OSMSG_WORKERS` | cpu count | Parallel parse workers. |
| `--cache-dir` | `OSMSG_CACHE_DIR` | platform cache | Where downloaded OSM files are kept across runs. |
| `--output-dir` | `OSMSG_OUTPUT_DIR` | `.` | Where `<name>.duckdb` and exports are written. |
| `--format` / `-f` | `OSMSG_FORMAT` | `parquet` | Repeat for multiple. Comma-separated when set via env. |
| `--overwrite` | (none) | off | Recompute even if `<name>.duckdb` already holds this exact query. |
| `--psql-dsn` | `OSMSG_PSQL_DSN` | unset | libpq DSN for `-f psql`. |
| `--psql-bulk` | `OSMSG_PSQL_BULK` | off | Faster first full load to Postgres. |
| `--history` / `--no-history` | `OSMSG_HISTORY` | on | Read covered months from the published dataset. |
| `--history-url` | `OSMSG_HISTORY_URL` | `osmsg-history` | Published dataset location. |
| `--insert` | (none) | off | Load history into the store and seed resume, then exit. No window loads all of it. |
| `--osh-file` / `--changeset-file` | (none) | unset | Insert from local planet history + changeset files. |
| `--changeset-pad-hours` | `OSMSG_CHANGESET_PAD_HOURS` | `1` | See below. |
| (auto-bootstrap on `--update`) | `OSMSG_BOOTSTRAP` | `hour` | `hour`, `day`, or `week`. Used when `--update` runs against an empty DB. |
| (auto-bootstrap on `--update`) | `OSMSG_BOOTSTRAP_DAYS` | unset | Integer N; overrides `OSMSG_BOOTSTRAP`. |
| OSM credentials (Geofabrik) | `OSM_USERNAME`, `OSM_PASSWORD` | unset | Required only when a Geofabrik URL is in use. |

A `.env` file at the working directory is loaded automatically.

## Maintainers

Generating and publishing the history dataset is the `osmsg maintain` group:

```bash
osmsg maintain month 2026-06 --repo osgeonepal/osmsg-history   # append one finished month
osmsg maintain month 2026-06 --no-upload                       # generate locally, review, upload later
osmsg maintain convert history.osh.pbf changesets.osm.bz2 2005-01-01 2026-06-01 work --parts 24
osmsg maintain publish work/out --repo osgeonepal/osmsg-history
```

See [experiments/parquet-history](./experiments/parquet-history/README.md) for the full-history batch.

## Documentation

- [Installation](./docs/Installation.md)
- [Manual](./docs/Manual.md) (every flag, with examples)
- [Self-hosting / Docker Compose](./docs/infra.md)
- [Version control / release notes](./docs/Version_control.md)

## Contributing

Pull requests are welcome. Quick path:

```bash
git clone https://github.com/osgeonepal/osmsg && cd osmsg
git switch develop
uv sync
uv run pre-commit install
uv run pytest -m "not network"
```

Please read [CONTRIBUTING.md](./CONTRIBUTING.md) and the [Code of Conduct](./CODE_OF_CONDUCT.md) before opening a PR.
Use [Conventional Commits](https://www.conventionalcommits.org/) (`cz commit`).

## License

[MIT](./LICENSE) © OSGeo Nepal contributors.
