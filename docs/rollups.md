# Rollups and the query engine

osmsg answers window, hashtag, and tag queries over the whole OSM history in seconds. It keeps the
recent tail live and serves everything older from small precomputed aggregates published on
HuggingFace next to the raw `changefiles` and `changesets`. One query module computes every number,
so the CLI, the Python library, and the API return identical results.

## Why

An all-time leaderboard has no time filter, so a naive query reads every changeset (180M+ rows), which
takes tens of seconds to minutes and grows with history. The same answer from a precomputed per-user
aggregate is milliseconds. A hashtag page over the full history reads only that hashtag's slice.
Rollups trade a fixed monthly build for fast reads.

## Datasets on HuggingFace

Under `hf://datasets/kshitijrajsharma/osmsg-history`:

| Path | Grain | Serves |
| --- | --- | --- |
| `changefiles` | per-changeset counts + `tag_stats` + `created_at` + centroid | the source of truth for counts |
| `changesets` | per-changeset uid/username/editor/hashtags + centroid | the source of truth for metadata |
| `rollup/hashtag_changeset` | one row per (hashtag, changeset), full breakdown + native `tags` | any hashtag query, exact |
| `rollup/alltime_user` | one row per uid | the all-time leaderboard with no hashtag |
| `rollup/users` | uid to username | display names, joined at read time |

`manifest.json` records `min_month` and `max_month`. The frontier is the first instant after
`max_month`; history is everything before it.

### hashtag_changeset

One row per `(hashtag, changeset_id)`, sorted by lowercased hashtag so a prefix range prunes row
groups. Columns: `hashtag`, `changeset_id`, `uid`, `editor`, `created_at`, the eleven count columns
(`nodes_created` … `poi_modified`), and `tags`. `tags` is a native `LIST<STRUCT(k, v, c, m, len_m)>`
(pre-exploded tag stats), so the tag breakdown reads a column instead of parsing JSON per query.

Keeping `changeset_id` makes any prefix exact: a query dedups by `changeset_id`, so a changeset that
carries two matching hashtags counts once. Summing an aggregate that dropped `changeset_id` would
double-count such changesets, so the long form is used.

## The query surface

`osmsg.stats` is the single definition of what a stat is: the count columns, `map_changes` (nodes,
ways, relations; poi excluded), the per-user aggregation, and the tag breakdown. It emits SQL that
runs on DuckDB and on Postgres, so the same query returns the same numbers on either engine.

### Tag representation

Tags are stored one way in every queried store: a native list of `(key, value, creates, modifies,
length_m)`. In DuckDB (the store and the rollup) that is a `LIST<STRUCT>`; in Postgres it is an array
of the composite type `osmsg_tag`. A tag breakdown reads that column directly, the same code on either
engine, with no per-query JSON parsing. JSON survives only as the archival wire form of the published
`changefiles` dataset and is converted to the native list on read (`osmsg.stats.tag_list_expr`). An
existing Postgres deployment moves to the native column once with `docs/migrate_pg_native_tags.sql`; a
fresh deployment creates it on first start.

`osmsg.catalog` combines two sources into one relation, split at the frontier: history comes from the
`hashtag_changeset` rollup, and the recent tail (from the frontier on) is derived on the fly from the
base `changeset_stats`/`changesets` tables, filtered by the requested hashtag first so only matching
changesets are read. There is no materialized recent rollup, so the recent side is always as fresh as
the store, and `osmsg --update` alone keeps queries current. The split by `created_at` means no
changeset is in both, so nothing is counted twice and nothing is dropped.

`osmsg.query` builds the hashtag page from these: `summary`, `leaderboard`, `tags`, `editors`. The CLI,
the library, and the API call it, so a number computed one way matches the others.

## Deployment modes

The engine is the same; only where the tables live changes. Pick per deployment.

### Pure DuckDB

One local store holds the whole history. Load it once from the published dataset, then keep it current
from live diffs:

    osmsg --insert                 # load all published history into <name>.duckdb, seed resume state
    osmsg --update                 # catch up from the frontier to now, then again on a schedule

Queries read the local store. No network at query time.

### Postgres plus DuckDB (light)

Postgres holds only the recent tail; history stays remote on HuggingFace. Seed the resume point
without pulling history, then catch up the tail:

    osmsg --insert --seed-only     # seed resume state at the frontier, no history ingested
    osmsg --update --format psql --psql-dsn <dsn>   # catch up the tail into Postgres

At query time DuckDB attaches Postgres, derives the recent tail from its base tables filtered by the
hashtag, and reads history from the HuggingFace artifact, combined by the catalog. Nothing is
materialized or refreshed on a timer: the recent side reflects Postgres as of each request, so
`osmsg --update` into Postgres is enough to keep the API current.

## Live updates

`osmsg --update` resumes from the store's state and runs to the replication head. Run it on a schedule
(for example every minute or two) to stay current. It auto-selects granularity by the size of the gap
and refines from day to hour to minute as it catches up.

## Building and publishing

`osmsg maintain month <YYYY-MM>` builds a finished month: it runs osmsg over the month from day diffs,
exports the `changefiles` and `changesets` partitions, refreshes `hashtag_changeset`, `alltime_user`,
and `users`, and uploads them. It refuses a month that stopped short of its boundary, so published
months are complete. Re-running a month rebuilds and overwrites it.

## Consistency

- A changeset belongs to exactly one month by `created_at`, so summing months is exact.
- History and recent are split at the frontier, so combining them is exact.
- The same `osmsg.stats` definitions run on DuckDB and Postgres, so the backend is a choice, not a
  behaviour change.
- Measured against ohsomeNow for hotosm* in 2024: contributors within 0.15%, buildings created within
  0.14%. Edit totals differ by design, because ohsome propagates geometry (moving a node marks the
  parent way modified) while osmsg counts direct element-version bumps.
