#!/usr/bin/env python3
"""Compare osmsg's v2 API against ohsomeNow for a hashtag over an optional window.

Compares the metrics whose definitions coincide (contributors, changesets, buildings created) and
labels the ones that differ by design: osmsg counts every element-version bump, while ohsome propagates
geometry (moving a node marks the parent way modified), so "edits" and "buildings modified" are not
comparable.

Usage:
  scripts/ohsome_compare.py hotosm --start 2026-07-01 --end 2026-07-25
  scripts/ohsome_compare.py hotosm --api-base https://api.osmsg.osgeonepal.org
"""

import argparse
import sys
import urllib.parse
import urllib.request

OSMSG_DEFAULT = "https://api.osmsg.osgeonepal.org"
OHSOME_DEFAULT = "https://stats.now.ohsome.org"


def _get_json(url: str, timeout: int) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 (trusted hosts)
        import json

        return json.load(response)


def _osmsg(api_base: str, hashtag: str, path: str, start: str | None, end: str | None, timeout: int) -> dict:
    params = {k: v for k, v in (("start", start), ("end", end)) if v}
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    return _get_json(f"{api_base}/api/v2/hashtag/{hashtag}/{path}{query}", timeout)


def _ohsome(ohsome_base: str, hashtag: str, start: str | None, end: str | None, timeout: int) -> dict:
    params = {"hashtag": hashtag, "topics": "contributor,changeset,building"}
    if start:
        params["startdate"] = start
    if end:
        params["enddate"] = end
    url = f"{ohsome_base}/api/stats?{urllib.parse.urlencode(params)}"
    return _get_json(url, timeout)["result"]["topics"]


def _buildings_created(tags: list[dict]) -> int:
    return sum(r["creates"] for r in tags if r["tag_key"] == "building")


def _row(label: str, ours: float, theirs: float, comparable: bool) -> str:
    delta = "" if not theirs else f"{(ours - theirs) / theirs * 100:+.2f}%"
    note = "" if comparable else "  (differs by design)"
    return f"  {label:<20} osmsg={ours:>14,.0f}  ohsome={theirs:>14,.0f}  {delta:>9}{note}"


def _iso(value: str | None) -> str | None:
    """Normalise a bare date (YYYY-MM-DD) to a full UTC timestamp the APIs accept."""
    return f"{value}T00:00:00Z" if value and len(value) == 10 else value


def compare(hashtag: str, start: str | None, end: str | None, api_base: str, ohsome_base: str, timeout: int) -> None:
    start, end = _iso(start), _iso(end)
    summary = _osmsg(api_base, hashtag, "summary", start, end, timeout)
    tags = _osmsg(api_base, hashtag, "tags", start, end, timeout)
    oh = _ohsome(ohsome_base, f"{hashtag}*", start, end, timeout)

    window = f"{start or 'all'} .. {end or 'now'}"
    print(f"\n#{hashtag}  [{window}]\n")
    print(_row("contributors", summary["users"], oh["contributor"]["value"], True))
    print(_row("changesets", summary["changesets"], oh["changeset"]["value"], True))
    print(_row("buildings created", _buildings_created(tags), oh["building"]["added"], True))
    print(_row("edits / map_changes", summary["map_changes"], oh.get("edit", {}).get("value", 0), False))
    print(_row("buildings modified", sum(r["modifies"] for r in tags if r["tag_key"] == "building"),
              oh["building"]["modified"]["count_modified"], False))
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare osmsg v2 API stats against ohsomeNow.")
    parser.add_argument("hashtag", help="Hashtag prefix without '#', e.g. 'hotosm'.")
    parser.add_argument("--start", help="Window start (ISO, e.g. 2026-07-01). Omit for all-time.")
    parser.add_argument("--end", help="Window end (ISO). Omit for now.")
    parser.add_argument("--api-base", default=OSMSG_DEFAULT, help=f"osmsg API base (default {OSMSG_DEFAULT}).")
    parser.add_argument("--ohsome-base", default=OHSOME_DEFAULT, help=f"ohsomeNow base (default {OHSOME_DEFAULT}).")
    parser.add_argument("--timeout", type=int, default=150, help="Per-request timeout seconds.")
    args = parser.parse_args(argv)
    try:
        compare(args.hashtag, args.start, args.end, args.api_base, args.ohsome_base, args.timeout)
    except Exception as exc:  # noqa: BLE001 (top-level CLI guard)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
