#!/usr/bin/env bash
# Planet batch: download once, time-filter (C++), convert out-of-core to changefiles/changesets parquet.
# Server job, not in-session: the history file is ~148 GB and the streaming pass is hours.
#
# Usage: ./planet_batch.sh <start YYYY-MM-DD> <end YYYY-MM-DD> [history_url]
#
# history_url defaults to the planet full-history directory's latest file. Confirm the current name at
# https://planet.openstreetmap.org/pbf/full-history/ (the dated history-YYMMDD.osh.pbf). Needs the
# osmium CLI (osmium-tool) for time-filter and ~150 GB free for the download plus the windowed extract.
set -euo pipefail

start="${1:?start date YYYY-MM-DD}"
end="${2:?end date YYYY-MM-DD}"
history_url="${3:-https://planet.openstreetmap.org/pbf/full-history/history-latest.osm.pbf}"
changeset_url="https://planet.openstreetmap.org/planet/changesets-latest.osm.bz2"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$here/planet_work"
mkdir -p "$work"

command -v osmium >/dev/null || { echo "osmium-tool required (time-filter). Install osmium-tool."; exit 1; }

echo ">>> [1/4] download history + changeset dump (resumable)"
curl -fSL -C - -o "$work/history.osh.pbf" "$history_url"
curl -fSL -C - -o "$work/changesets.osm.bz2" "$changeset_url"

echo ">>> [2/4] time-filter history to [$start, $end] (C++, avoids Python touching all versions)"
osmium time-filter -O -o "$work/history-window.osh.pbf" \
    "$work/history.osh.pbf" "${start}T00:00:00Z" "${end}T00:00:00Z"

echo ">>> [3/4] stream + aggregate out-of-core to parquet"
uv run --project "$here/../.." osmsg maintain convert \
    "$work/history-window.osh.pbf" "$work/changesets.osm.bz2" "$start" "$end" "$work" --parts 24

echo ">>> [4/4] done. datasets in $work/out/{changefiles,changesets}."
echo "    publish: uv run --project $here/../.. osmsg maintain publish $work/out --repo <repo>"
