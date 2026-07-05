#!/usr/bin/env bash
# Publish the previous finished month to the HuggingFace history dataset. Scratch stays on the
# block volume so the root disk is never touched.
set -euo pipefail

cd /opt/osmsg-maintain

repo="${OSMSG_HISTORY_REPO:-kshitijrajsharma/osmsg-history}"
ym="${1:-$(date -u -d "$(date -u +%Y-%m-01) -1 day" +%Y-%m)}"

work=/mnt/mnt/osmsg/maintain/work
out=/mnt/mnt/osmsg/maintain/out
mkdir -p "$work" "$out"

echo "[maintain] publishing month ${ym} to ${repo}"
exec uv run osmsg maintain month "${ym}" --repo "${repo}" --work-dir "$work" --output-dir "$out"
