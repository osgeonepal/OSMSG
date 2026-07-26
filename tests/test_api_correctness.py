"""End-to-end number regression guard: pinned stats for fixed hashtag+window queries.

Each case uses a window entirely inside published history (before the frontier), where the rollup is
immutable, so the numbers are reproducible run to run; a drift beyond `OSMSG_API_NUM_TOLERANCE` (default
1%) flags a real regression. Runs against a live API (`OSMSG_API_BASE`); `network`-marked. Add mega-
hashtag pins here once they return under the latency ceiling.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.network

requests = pytest.importorskip("requests")

_BASE = os.environ.get("OSMSG_API_BASE", "https://api.osmsg.osgeonepal.org").rstrip("/")
_TOLERANCE = float(os.environ.get("OSMSG_API_NUM_TOLERANCE", "0.01"))
_TIMEOUT = 60

# (hashtag, start, end, expected). History-only windows -> immutable published rollup -> stable numbers.
_PINS = [
    (
        "osmnepal",
        "2023-01-01T00:00:00Z",
        "2024-01-01T00:00:00Z",
        {"users": 1460, "changesets": 30648, "map_changes": 6086506, "nodes_created": 3638468},
    ),
    (
        "osmnepal",
        "2020-01-01T00:00:00Z",
        "2021-01-01T00:00:00Z",
        {"users": 436, "changesets": 12525, "map_changes": 2865290, "nodes_created": 1893006},
    ),
]


@pytest.mark.parametrize(
    ("hashtag", "start", "end", "expected"),
    _PINS,
    ids=[f"{h}-{s[:4]}" for h, s, _e, _x in _PINS],
)
def test_summary_numbers_stable(hashtag: str, start: str, end: str, expected: dict[str, int]):
    resp = requests.get(
        f"{_BASE}/api/v2/hashtag/{hashtag}/summary", params={"start": start, "end": end}, timeout=_TIMEOUT
    )
    assert resp.status_code == 200, f"HTTP {resp.status_code}"
    got = resp.json()
    drifted = []
    for key, want in expected.items():
        have = got.get(key)
        if have is None or abs(have - want) > max(1, want * _TOLERANCE):
            drifted.append(f"{key}: expected {want:,}, got {have:,} (>{_TOLERANCE:.1%})")
    assert not drifted, f"{hashtag} [{start}..{end}) drifted -> " + "; ".join(drifted)
