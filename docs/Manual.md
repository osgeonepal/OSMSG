# Manual

The full flag reference, grouped by what you're trying to do. New here? Start with the
[README quick start](../README.md#quick-start), then come back for the details.

## Time range

```bash
osmsg --last hour|day|week|month|year
osmsg --days 7
osmsg --start "2026-04-01 00:00:00" --end "2026-04-08 00:00:00"
osmsg --update                       # resume from last finished run in <name>.duckdb
                                     # (must use the same --url as the prior run; switching
                                     # granularity would double-count via changeset_stats)
```

> Times are UTC.

## Source

```bash
osmsg --url minute|hour|day          # planet replication shortcuts
osmsg --url https://...              # any OSM replication base
osmsg --country nepal --country india --country africa   # Geofabrik regions, resolved live
```

> When `--url` is omitted, osmsg picks a planet replication granularity that fits the requested
> span: minute for spans under 6h, hour for 6h to 7d, day for 7d or more. A warning prints when the
> auto-switch happens; pass `--url` explicitly to override (also suppressed by `--country`,
> `--update`, or multiple `--url` values).

## Filters

```bash
osmsg --hashtags hotosm-project-1234 --hashtags mapathon
osmsg --hashtags mapathon --exact-lookup       # match whole hashtag, not substring
osmsg --users alice --users bob
osmsg --boundary nepal                         # Geofabrik region name
osmsg --boundary region.geojson               # path to a GeoJSON file
osmsg --boundary '{"type":"Polygon",...}'     # inline GeoJSON string
```

> `--boundary` filters changesets whose bounding box intersects the given geometry.
> A Geofabrik region name resolves from the same index as `--country`, no separate file needed.
> `--boundary` only filters; it does not change the replication source.
> To scope the replication source to a country's diffs, use `--country` instead.
>
> Each `--users`, `--hashtags`, `--tags`, `--length`, `--country`, `--url`, `-f`
> takes one value at a time; pass the flag again for additional values.
>
> Editor stats are always included when `--changeset` or `--hashtags` is on:
> the `editors` column lists every `created_by` tag the user appeared with.

## Tag stats

```bash
osmsg --tags building --tags highway           # per-key create/modify counts
osmsg --length highway --length waterway       # length in metres for created ways
osmsg --keys                                   # every tag key (no value breakdown)
osmsg --all                                    # every key=value combo + changeset metadata (hashtags, editors)
```

## Output

```bash
osmsg --last day -f parquet                    # default; one columnar file
osmsg --last day -f csv -f json -f markdown
osmsg --last day --summary                     # daily breakdown in each requested format
osmsg --last day -f psql --psql-dsn "host=localhost dbname=osm user=osm"
```

> Every run writes `<name>.duckdb` plus the formats you ask for. Parquet is the canonical exchange:
> open with DuckDB, polars, or pandas directly.
>
> `--summary` follows the same `-f` formats: requesting `-f csv --summary` produces both `<name>.csv`
> and `<name>_summary.csv`. The `psql` target is intentionally skipped for summary, since the daily
> breakdown is just a query over the four base tables, so consumers derive it on demand instead of
> duplicating data.

`<name>.duckdb` is stamped with the query that built it. Rerunning the same query with a different `-f`
re-exports straight from that store, so adding a format is instant. Changing any query parameter
(window, hashtags, tags, boundary) recomputes; `--overwrite` forces a fresh recompute.

## Config file

Long invocations are easier to maintain in YAML. Each key is the option's underscore name (the flag
with `-` turned to `_`, so `output_dir`, `history_url`, `psql_dsn`); `--all` is `all_stats` and
`--keys` is `keys_only`. A key that matches no option is ignored.

```bash
osmsg --config nepal.yaml                      # all flags from yaml
osmsg --config nepal.yaml --rows 50            # CLI overrides yaml
```

```yaml
# nepal.yaml
name: nepal_weekly
country: [nepal]
last: week
hashtags:
  - hotosm-project-1234
  - mapathon
users: [alice, bob, charlie]
tags: [building, highway]
formats: [parquet, markdown]
summary: true
update: true
```

## Caching

Downloaded `.osc.gz` files cache to a per-user dir (`~/Library/Caches/osmsg` on macOS,
`~/.cache/osmsg` on Linux). Re-running the same range reuses them, so no network is needed.
`--cache-dir` to relocate, `--delete-temp` to clean up after a run.

## Setting up a store

`--insert` loads history into the store and seeds the resume position, then exits. Follow it with
`--update` to catch up to now and keep current. DuckDB is the default store; pass `--psql-dsn` to use
Postgres (no separate `-f psql` needed).

```bash
osmsg --insert                                   # load all published history into stats.duckdb
osmsg --update                                   # catch up to now, then run on cron

osmsg --insert --psql-dsn "host=localhost dbname=osm user=osm"   # into Postgres (bulk first load)
osmsg --insert --start 2020-01-01 --end 2023-01-01               # a slice; --update continues from its end
osmsg --insert --osh-file history.osh.pbf --changeset-file changesets.osm.bz2  # from local files
```

- No window loads the whole dataset; `--start/--end` loads a slice and resumes from the slice end.
- `--osh-file` with `--changeset-file` converts local planet files into the store (offline, or a custom
  extract). Give both together.
- The Postgres load uses the bulk path (drops indexes and keys, rebuilds after).

`--insert` and `--update` pick the replication granularity from how far behind the store is. A fresh
store clears the multi-week backlog on day diffs (tens of files), then refines to hour and minute as it
stays current. For near-real-time, run `osmsg --update --url minute`. A store tracks one granularity at
a time; changing it hands off at the day boundary, so the windows stay disjoint. Pass `--url` to either
command to set the granularity yourself.

## Cloud-native history

Months covered by a published parquet dataset (default `kshitijrajsharma/osmsg-history` on
HuggingFace) are read remotely. The recent uncovered tail uses the live replication path. This is on
by default.

```bash
osmsg --start 2015-01-01 --end 2020-01-01     # read from the dataset
osmsg --start 2024-01-01                       # covered months remote, current month live
osmsg --last week --no-history                  # live path only
```

- `--no-history` (env `OSMSG_HISTORY=0`) uses the live path.
- `--history-url` (env `OSMSG_HISTORY_URL`) sets the dataset location.
- The live path is used when the dataset is unreachable, with `--update`, and with `--length`.

### Postgres as a source of truth

`osmsg --insert --psql-dsn ...` loads the dataset into osmsg's schema and seeds the resume position;
`osmsg --update --psql-dsn ...` then keeps Postgres current. `--psql-bulk` (env `OSMSG_PSQL_BULK`)
forces the bulk path on a plain run; `--insert` already uses it.

## Maintaining the dataset

`osmsg maintain` builds and publishes the history parquet.

```bash
osmsg maintain month 2026-06 --repo osgeonepal/osmsg-history   # build one finished month and upload
osmsg maintain month 2026-06 --no-upload                       # build locally, review, upload later
osmsg maintain publish out --repo osgeonepal/osmsg-history     # write + upload manifest.json
osmsg maintain convert history.osh.pbf changesets.osm.bz2 2005-01-01 2026-06-01 work --parts 24
```

`month` builds from the live day diffs, exports the two partitions, uploads, and advances the
manifest. It refuses to publish a month whose data stops short of the month boundary (pass
`--allow-incomplete` to override), so published months are complete by construction. Re-running
`osmsg maintain month <YYYY-MM>` rebuilds a month and overwrites its published partition, which repairs
a month that was first generated from a mid-day planet snapshot. `convert` turns local planet files
into the datasets out of core. Uploads use the `hf` CLI (`uvx`), so be logged in to HuggingFace.

## Credentials

`--country` and any `geofabrik` URL need OSM credentials. Resolution order:

1. `--username` (CLI) + `OSM_PASSWORD` env var, or `--password-stdin` to pipe a password in
   (e.g. `cat secret | osmsg --password-stdin ...`)
2. `OSM_USERNAME` + `OSM_PASSWORD` env vars (auto-loaded from `.env`)
3. Interactive prompt (TTY only)

> The CLI does not accept `--password` directly, because passwords on the command line leak into
> shell history and `ps` output. Use stdin or env vars.

## Recipes

```bash
# Daily Nepal stats with summary
osmsg --country nepal --last day --summary --tags building --tags highway

# Mapathon report (hashtag substring, last 6 days, with TM totals)
osmsg --hashtags smforst --days 6 --summary --tm-stats

# Full year of global stats to Postgres (incremental-friendly)
osmsg --start "2025-01-01 00:00:00" --end "2026-01-01 00:00:00" \
      --url day --all -f parquet -f psql \
      --psql-dsn "host=localhost dbname=osm_stats user=osm"

# All-time Nepal stats via planet/day (Geofabrik only keeps ~4 months per country)
osmsg --url day --boundary nepal --start "2012-09-13" -f parquet -f psql ...

# Cron / systemd: refresh Nepal nightly
osmsg --country nepal --update
```

> `map_changes` per row is the sum of the nine element columns
> (`{nodes,ways,rels}_{created,modified,deleted}`); POI counters are tracked separately.
