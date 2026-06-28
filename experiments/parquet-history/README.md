# Historical parquet datasets

osmsg precomputes OSM history into time-partitioned parquet published on HuggingFace, so past-window
queries are served remotely from the published parquet. The read side is built into osmsg
(`osmsg --start ... --end ...` serves covered months from the dataset, `osmsg --insert` loads it into
a store). The build and publish side is the `osmsg maintain` subcommand group; this directory holds
only the server batch script that wraps it.

## Datasets

Two datasets, both partitioned `year=*/month=*` and Morton(centroid)-sorted for time and bbox pruning:

- **changefiles**, per-changeset counts + poi + `tag_stats` JSON + `created_at` + bbox.
- **changesets**, per-changeset metadata: uid, username, `created_at`, editor, hashtags, bbox.

## Build the full history (server batch)

```bash
./planet_batch.sh <start YYYY-MM-DD> <end YYYY-MM-DD>
```

Downloads `history-latest.osm.pbf` + `changesets-latest.osm.bz2`, time-filters with `osmium`, then
runs `osmsg maintain convert` to stream and aggregate out of core. Needs the `osmium` CLI and ~150 GB
free. The conversion alone is `osmsg maintain convert <osh> <changesets> <start> <end> <work_dir>
--parts N`.

## Publish and maintain

```bash
osmsg maintain publish <out_dir> --drop-last --repo <repo>   # write + upload manifest.json
osmsg maintain month <YYYY-MM> --repo <repo>                 # append one finished month
osmsg maintain month <YYYY-MM> --no-upload                   # generate locally, review, upload later
```

`osmsg maintain month` builds the month from the live day diffs, exports the two partitions, uploads
them, and advances the manifest. It refuses to publish a month that stops short of its boundary
(`--allow-incomplete` overrides), and re-running it rebuilds and overwrites a month, which repairs one
first generated from a mid-day planet snapshot.

## Load into a store

`osmsg --insert` loads the published parquet into an osmsg DuckDB or Postgres store and seeds the
resume position; see the [Manual](../../docs/Manual.md).
