# Way length (`l`) and the history backfill

osmsg records the length of open ways per tag. Each tag in the `changeset_stats.tags` struct carries
`l`, the way length in metres (`STRUCT(k, v, c, m, l)`). The API `/tags` endpoint returns `length_m`
per tag and the leaderboard UI shows it as a kilometre badge.

## What gets a length

A way gets a length when it is **open** (its first and last node references differ) and on **create**.
The length is the haversine distance along the way's nodes, attached to every tag of that way. Closed
ways (buildings, areas, roundabouts) get no length. A computed length above `MAX_WAY_LENGTH_M`
(2,000 km) is dropped as a geometry error (for example an early node first uploaded at (0,0) and later
corrected).

Geometry source: osmium's node index keeps each node's first-seen coordinates, so a way is measured
with near-creation-time geometry. On a sample of real ways this sits within about 2% of the exact
creation-time length in aggregate.

## Accuracy: length is a lower bound

Measuring an open way needs the coordinates of every node it references. A diff carries coordinates
only for nodes touched in that diff, so a way that connects to a pre-existing node (a junction it was
drawn onto) has an unresolved node. osmium raises `InvalidLocationError` for the whole way and it is
skipped, contributing zero length. The reported length is therefore a lower bound of the true
geometry length.

The gap depends on how much new geometry connects into existing data. Remote greenfield lines, whose
nodes are all new in the same diff, are measured almost completely; urban lines wired into existing
streets lose more. Against full-geometry tools such as ohsome, osmsg highway kilometres run below
their road length, and by a wider margin for urban work. This is a definitional lower bound, not a
scaling error.

The only path that resolves pre-existing nodes is the full-planet single pass in the History backfill
section below, which builds a global node index. The live tick and the monthly `maintain month`
append both read diffs and share this lower-bound behaviour.

## Live path

The worker computes length on every tick: it applies changefiles with node locations on, and measures
open-way creates whose nodes are present in the same diff. A way whose nodes come from an earlier diff
has no location in the current diff and is skipped.

## History backfill

The published history has no length until the datasets are regenerated, because the length needs node
coordinates that the earlier conversion did not read.

`osmsg maintain convert` (and `convert()` in `osmsg/maintain/convert.py`) now streams the history in a
**single pass** with a file-backed node index, so open-way lengths can be measured. The parallel
blob-split path is not used: a way's nodes can live in another split part, and osmium
`add-locations-to-ways` rejects history files, so a global node index in one pass is required.

Run on a large machine:

```
osmsg maintain convert --osh history-latest.osh.pbf --changesets changesets-latest.osm.bz2 \
  --start <min> --end <max> --work-dir /data/convert
```

Then republish the two datasets and the rollup to HuggingFace, and bump the manifest.

### Cost

- Disk: the node index is roughly 100-150 GB for the full planet (one entry per node id). Keep it on
  the work volume, not the OS disk.
- Time: the single streaming pass is single-threaded. The earlier parallel (24-part) streaming ran in
  about 8.4 h; without that parallelism the streaming is substantially longer (plan for the order of a
  day), plus aggregation and export. Size the run accordingly.
- Optimisation (not built): pre-build the node index once, then let parallel part-readers use it
  read-only. This restores parallelism and is the way to cut the single-pass time.

## Deployment order (load-bearing)

The field rename `len_m` -> `l` changes the schema, so the renamed code and the republished data must
land together. Deploying the renamed reader against data that still has `len_m` fails.

1. Backfill and republish the rollup, changefiles, and changesets with `l`.
2. Migrate the production Postgres: run `docs/migrate_pg_len_to_l.sql` once (renames the `osmsg_tag`
   composite attribute). A fresh store self-creates the type with `l`.
3. Deploy the renamed worker and API images.

Until all three are done, the current `len_m` data and the old images stay in place.
