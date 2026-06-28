"""Tasking Manager API integration (HOT TM4)."""

from __future__ import annotations

import concurrent.futures
import re
from collections import defaultdict
from typing import Any

from ._http import session

TM_API = "https://tasking-manager-tm4-production-api.hotosm.org/api/v2/projects"
PROJECT_RE = re.compile(r"#hotosm-project-(\d+)")
_MAX_PARALLEL = 8


def extract_projects(hashtags: list[str] | str) -> list[str]:
    """Pull `hotosm-project-N` numeric ids from one or many hashtag strings."""
    text = ",".join(hashtags) if isinstance(hashtags, list) else hashtags or ""
    return PROJECT_RE.findall(text)


def _fetch_one(project_id: str) -> tuple[str, list[dict[str, Any]]]:
    try:
        r = session.get(f"{TM_API}/{project_id}/contributions/")
    except Exception:
        return project_id, []
    if r.status_code != 200:
        return project_id, []
    return project_id, r.json().get("userContributions", []) or []


def fetch_user_stats(project_ids: list[str], usernames: set[str]) -> dict[str, dict[str, Any]]:
    """Aggregate tasks_mapped/validated/total per matching user across all projects.

    Project requests are issued in parallel with a bounded thread pool, the public
    TM API is rate-limited but tolerant of small bursts; failures are absorbed
    silently so one bad project never breaks an entire run.
    """
    by_user: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {"tm_mapping_level": None, "tasks_mapped": 0, "tasks_validated": 0, "tasks_total": 0}
    )

    workers = min(_MAX_PARALLEL, max(1, len(project_ids)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for _, contributions in pool.map(_fetch_one, project_ids):
            for u in contributions:
                name = u.get("username")
                if name not in usernames:
                    continue
                agg = by_user[name]
                agg["tm_mapping_level"] = agg["tm_mapping_level"] or u.get("mappingLevel")
                agg["tasks_mapped"] += u.get("mapped", 0)
                agg["tasks_validated"] += u.get("validated", 0)
                agg["tasks_total"] += u.get("total", 0)
    return dict(by_user)


def enrich(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach TM totals to each row by username; rows without project hashtags get zeros."""
    project_ids = sorted({pid for r in rows for pid in extract_projects(r.get("hashtags", []))})
    usernames = {r["name"] for r in rows}
    stats = fetch_user_stats(project_ids, usernames) if project_ids else {}

    for r in rows:
        agg = stats.get(r["name"])
        r["tm_mapping_level"] = agg["tm_mapping_level"] if agg else None
        r["tasks_mapped"] = agg["tasks_mapped"] if agg else 0
        r["tasks_validated"] = agg["tasks_validated"] if agg else 0
        r["tasks_total"] = agg["tasks_total"] if agg else 0
    return rows
