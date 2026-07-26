#!/usr/bin/env python3
"""Measure osmsg's v2 API speed (every endpoint timed vs --target-seconds) and accuracy (summary/tag
numbers vs ohsomeNow, vs --tolerance), exiting non-zero if anything is flagged so it doubles as a gate.

Usage: scripts/ohsome_compare.py hotosm --start 2026-07-01 --end 2026-07-25 --target-seconds 20
"""

import argparse
import sys
import time
import urllib.parse

import requests

OSMSG_DEFAULT = "https://api.osmsg.osgeonepal.org"
OHSOME_DEFAULT = "https://stats.now.ohsome.org"


def _get_json(url: str, timeout: int) -> dict:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _osmsg(
    api_base: str, hashtag: str, path: str, start: str | None, end: str | None, timeout: int, extra: str = ""
) -> tuple[dict | list, float]:
    params = {k: v for k, v in (("start", start), ("end", end)) if v}
    query = f"?{urllib.parse.urlencode(params)}{extra}" if params or extra else ""
    started = time.monotonic()
    data = _get_json(f"{api_base}/api/v2/hashtag/{hashtag}/{path}{query}", timeout)
    return data, time.monotonic() - started


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


_SPEED_ENDPOINTS = [
    ("summary", ""),
    ("leaderboard", "&page_size=10"),
    ("tags", ""),
    ("editors", ""),
    ("trends", "&interval=day"),
    ("hashtags", ""),
]


def compare(
    hashtag: str,
    start: str | None,
    end: str | None,
    api_base: str,
    ohsome_base: str,
    timeout: int,
    target_seconds: float,
    tolerance: float,
) -> int:
    """Print speed + accuracy for the hashtag; return the number of flagged (over-target / over-tolerance)
    checks so the caller can gate on it."""
    start, end = _iso(start), _iso(end)
    window = f"{start or 'all'} .. {end or 'now'}"
    print(f"\n#{hashtag}  [{window}]")
    flags = 0

    print(f"\n  speed (target <= {target_seconds:g}s):")
    results: dict[str, dict | list] = {}
    for path, extra in _SPEED_ENDPOINTS:
        data, elapsed = _osmsg(api_base, hashtag, path, start, end, timeout, extra)
        results[path] = data
        over = elapsed > target_seconds
        flags += over
        print(f"    {path:<14} {elapsed:>6.1f}s   {'OVER' if over else 'ok'}")

    print(f"\n  accuracy vs ohsomeNow (tolerance {tolerance:.0%}):")
    oh = _ohsome(ohsome_base, f"{hashtag}*", start, end, timeout)
    summary, tags = results["summary"], results["tags"]
    for label, ours, theirs, comparable in (
        ("contributors", summary["users"], oh["contributor"]["value"], True),
        ("changesets", summary["changesets"], oh["changeset"]["value"], True),
        ("buildings created", _buildings_created(tags), oh["building"]["added"], True),
        ("edits / map_changes", summary["map_changes"], oh.get("edit", {}).get("value", 0), False),
    ):
        line = _row(label, ours, theirs, comparable)
        if comparable and theirs and abs(ours - theirs) / theirs > tolerance:
            flags += 1
            line += "  OVER"
        print(line)
    print()
    return flags


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure osmsg v2 API speed and accuracy vs ohsomeNow.")
    parser.add_argument("hashtag", help="Hashtag prefix without '#', e.g. 'hotosm'.")
    parser.add_argument("--start", help="Window start (ISO, e.g. 2026-07-01). Omit for all-time.")
    parser.add_argument("--end", help="Window end (ISO). Omit for now.")
    parser.add_argument("--api-base", default=OSMSG_DEFAULT, help=f"osmsg API base (default {OSMSG_DEFAULT}).")
    parser.add_argument("--ohsome-base", default=OHSOME_DEFAULT, help=f"ohsomeNow base (default {OHSOME_DEFAULT}).")
    parser.add_argument("--target-seconds", type=float, default=20.0, help="Flag endpoints slower than this.")
    parser.add_argument("--tolerance", type=float, default=0.05, help="Flag comparable metrics off by more than this.")
    parser.add_argument("--timeout", type=int, default=150, help="Per-request timeout seconds.")
    args = parser.parse_args(argv)
    try:
        flags = compare(
            args.hashtag,
            args.start,
            args.end,
            args.api_base,
            args.ohsome_base,
            args.timeout,
            args.target_seconds,
            args.tolerance,
        )
    except (requests.RequestException, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if flags:
        print(f"{flags} check(s) flagged.", file=sys.stderr)
    return 1 if flags else 0


if __name__ == "__main__":
    raise SystemExit(main())
