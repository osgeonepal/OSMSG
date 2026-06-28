"""Tasking Manager integration: project id extraction, contribution fetch, enrichment."""

from __future__ import annotations

from typing import Any

import pytest

from osmsg import tm


class _StubResponse:
    def __init__(self, status: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def fake_tm_api(monkeypatch):
    """Replace tm.session.get with an in-memory dispatch table keyed by project id."""

    def install(by_project: dict[str, list[dict[str, Any]] | int]):
        def _get(url: str, **_):
            for pid, payload in by_project.items():
                if f"/{pid}/contributions" in url:
                    if isinstance(payload, int):  # status override
                        return _StubResponse(payload)
                    return _StubResponse(200, {"userContributions": payload})
            return _StubResponse(404)

        monkeypatch.setattr(tm.session, "get", _get)

    return install


def test_extract_projects_parses_multiple_hashtag_styles():
    assert tm.extract_projects(["#hotosm-project-123", "#mapathon"]) == ["123"]
    assert tm.extract_projects("#hotosm-project-1 #hotosm-project-2") == ["1", "2"]
    assert tm.extract_projects("") == []
    assert tm.extract_projects(None) == []  # type: ignore[arg-type]
    assert tm.extract_projects([]) == []


def test_fetch_user_stats_sums_across_projects(fake_tm_api):
    fake_tm_api(
        {
            "10": [
                {"username": "alice", "mappingLevel": "BEGINNER", "mapped": 5, "validated": 1, "total": 6},
                {"username": "bob", "mappingLevel": "ADVANCED", "mapped": 8, "validated": 0, "total": 8},
            ],
            "20": [
                {"username": "alice", "mappingLevel": "INTERMEDIATE", "mapped": 3, "validated": 2, "total": 5},
            ],
        }
    )
    out = tm.fetch_user_stats(["10", "20"], {"alice", "bob"})
    assert out["alice"]["tasks_mapped"] == 8  # 5 + 3
    assert out["alice"]["tasks_validated"] == 3
    assert out["alice"]["tasks_total"] == 11
    # mapping level: first non-null wins (parallel order is non-deterministic, both seen are valid)
    assert out["alice"]["tm_mapping_level"] in {"BEGINNER", "INTERMEDIATE"}
    assert out["bob"]["tasks_mapped"] == 8


def test_fetch_user_stats_filters_to_requested_usernames(fake_tm_api):
    fake_tm_api(
        {
            "1": [
                {"username": "alice", "mappingLevel": "BEGINNER", "mapped": 5, "validated": 0, "total": 5},
                {"username": "stranger", "mappingLevel": "ADVANCED", "mapped": 999, "validated": 0, "total": 999},
            ],
        }
    )
    out = tm.fetch_user_stats(["1"], {"alice"})
    assert "stranger" not in out
    assert out["alice"]["tasks_mapped"] == 5


def test_fetch_user_stats_swallows_404_for_one_project(fake_tm_api):
    fake_tm_api(
        {
            "good": [{"username": "alice", "mappingLevel": "BEGINNER", "mapped": 7, "validated": 0, "total": 7}],
            "missing": 404,
        }
    )
    out = tm.fetch_user_stats(["good", "missing"], {"alice"})
    assert out["alice"]["tasks_mapped"] == 7  # bad project did not abort the run


def test_enrich_attaches_zeros_when_no_project_hashtags():
    rows = [{"name": "alice", "hashtags": ["#mapathon"]}]
    out = tm.enrich(rows)
    assert out[0]["tasks_mapped"] == 0
    assert out[0]["tasks_validated"] == 0
    assert out[0]["tasks_total"] == 0
    assert out[0]["tm_mapping_level"] is None


def test_enrich_attaches_totals_for_users_in_project(fake_tm_api):
    fake_tm_api(
        {
            "42": [
                {"username": "alice", "mappingLevel": "ADVANCED", "mapped": 12, "validated": 4, "total": 16},
            ],
        }
    )
    rows = [
        {"name": "alice", "hashtags": ["#hotosm-project-42"]},
        {"name": "bob", "hashtags": ["#hotosm-project-42"]},  # not in project response
    ]
    out = tm.enrich(rows)
    by_name = {r["name"]: r for r in out}
    assert by_name["alice"]["tasks_mapped"] == 12
    assert by_name["alice"]["tm_mapping_level"] == "ADVANCED"
    # bob is in the row set but not in the API response → zeros
    assert by_name["bob"]["tasks_mapped"] == 0
    assert by_name["bob"]["tm_mapping_level"] is None
