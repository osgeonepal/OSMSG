"""End-to-end API latency guard: every v2 request must return under a ceiling (default 10s).

Runs against a live API (`OSMSG_API_BASE`, default the public deployment); `network`-marked so the
offline suite skips it. Each (endpoint, hashtag, window, filter) case is its own parametrization, so a
slow request flags exactly which shape regressed. Raise the ceiling with `OSMSG_API_MAX_SECONDS`.
"""

from __future__ import annotations

import datetime as dt
import os
import time

import pytest

pytestmark = pytest.mark.network

requests = pytest.importorskip("requests")

_BASE = os.environ.get("OSMSG_API_BASE", "https://api.osmsg.osgeonepal.org").rstrip("/")
_MAX_SECONDS = float(os.environ.get("OSMSG_API_MAX_SECONDS", "10"))
_TIMEOUT = _MAX_SECONDS + 20  # let a slow call finish so the assertion reports its real time


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


# Windows exercise every branch of the frontier split: all-time, rolling, history-only, recent-only.
_ALL_TIME: dict[str, str] = {}
_LAST_YEAR = {"start": _iso(_now() - dt.timedelta(days=365)), "end": _iso(_now())}
_LAST_30D = {"start": _iso(_now() - dt.timedelta(days=30)), "end": _iso(_now())}
_HISTORY_YEAR = {"start": "2024-01-01T00:00:00Z", "end": "2025-01-01T00:00:00Z"}
_RECENT_7D = {"start": _iso(_now() - dt.timedelta(days=7)), "end": _iso(_now())}

# hashtags span the range of cost: mega (hotosm), medium, event, and a multi-hashtag union.
_HASHTAGS = ["hotosm", "osmnepal", "2026_lach_ve_eq", "hotosm,osmnepal"]
_WINDOWS = {
    "all_time": _ALL_TIME,
    "last_year": _LAST_YEAR,
    "last_30d": _LAST_30D,
    "history_2024": _HISTORY_YEAR,
    "recent_7d": _RECENT_7D,
}
_ENDPOINTS = ["summary", "leaderboard", "tags", "editors", "trends", "hashtags", "map"]


def _cases():
    for endpoint in _ENDPOINTS:
        for hashtag in _HASHTAGS:
            for wname, window in _WINDOWS.items():
                yield pytest.param(endpoint, hashtag, window, id=f"{endpoint}-{hashtag}-{wname}")


def _get(path: str, params: dict[str, str]) -> tuple[int, float]:
    started = time.monotonic()
    resp = requests.get(f"{_BASE}{path}", params=params, timeout=_TIMEOUT)
    return resp.status_code, time.monotonic() - started


@pytest.mark.parametrize(("endpoint", "hashtag", "window"), list(_cases()))
def test_endpoint_under_ceiling(endpoint: str, hashtag: str, window: dict[str, str]):
    status, elapsed = _get(f"/api/v2/hashtag/{hashtag}/{endpoint}", dict(window))
    assert status == 200, f"{endpoint} {hashtag} {window} -> HTTP {status}"
    assert elapsed <= _MAX_SECONDS, f"{endpoint} {hashtag} {window} took {elapsed:.1f}s > {_MAX_SECONDS}s"


# Leaderboard carries the extra server-side controls (sort, order, paging, search); each must stay fast.
@pytest.mark.parametrize(
    "params",
    [
        {"sort": "map_changes", "order": "desc", "page": "1", "page_size": "10"},
        {"sort": "created", "order": "asc", "page": "3", "page_size": "25"},
        {"sort": "name", "order": "asc"},
        {"q": "a", "page_size": "10"},
    ],
    ids=["sort-map", "sort-created-p3", "sort-name", "search-a"],
)
def test_leaderboard_controls_under_ceiling(params: dict[str, str]):
    status, elapsed = _get("/api/v2/hashtag/osmnepal/leaderboard", params)
    assert status == 200, f"leaderboard {params} -> HTTP {status}"
    assert elapsed <= _MAX_SECONDS, f"leaderboard {params} took {elapsed:.1f}s > {_MAX_SECONDS}s"
