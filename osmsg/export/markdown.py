"""Markdown table + summary.md exporter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v)
    return str(v)


def table_markdown(rows: list[dict[str, Any]], output_path: Path, headers: list[str] | None = None) -> Path:
    """Return a GitHub-flavored markdown table for the given rows."""
    headers = headers or list(rows[0].keys())
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(_stringify(r.get(h)) for h in headers) + " |")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def _human(n: int) -> str:
    """Compact human number: 1234 -> 1.2K, 1500000 -> 1.5M."""
    n = int(n)
    for unit, scale in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if abs(n) >= scale:
            return f"{n / scale:.1f}{unit}".rstrip("0").rstrip(".")
    return str(n)


def _top_n(rows: list[dict[str, Any]], column: str, n: int = 5) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for r in rows:
        raw = r.get(column) or []
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if not item:
                continue
            counts[str(item).strip()] = counts.get(str(item).strip(), 0) + 1
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)[:n]


def summary_markdown(
    rows: list[dict[str, Any]],
    *,
    output_path: Path,
    start_date,
    end_date,
    additional_tags: list[str] | None = None,
    length_tags: list[str] | None = None,
    tag_mode: str = "none",
    fname: str = "stats",
    tm_stats: bool = False,
) -> Path:
    """Write the human-readable `summary.md` next to the parquet output."""
    n_users = len(rows)
    n_changesets = sum(int(r.get("changesets", 0) or 0) for r in rows)
    n_changes = sum(int(r.get("map_changes", 0) or 0) for r in rows)

    def _sum(*cols: str) -> int:
        return sum(sum(int(r.get(c, 0) or 0) for c in cols) for r in rows)

    created = _sum("nodes_created", "ways_created", "rels_created")
    modified = _sum("nodes_modified", "ways_modified", "rels_modified")
    deleted = _sum("nodes_deleted", "ways_deleted", "rels_deleted")

    parts: list[str] = []
    parts.append(f"### Stats from {start_date} to {end_date} (UTC)\n")
    parts.append(f"#### {_human(n_users)} users · {_human(n_changesets)} changesets · {_human(n_changes)} map changes")
    parts.append(f"#### Created {_human(created)} · Modified {_human(modified)} · Deleted {_human(deleted)}")
    parts.append(f"\nFull stats: `{fname}.parquet`")

    parts.append("\n#### Top 5 users")
    user_cols = (
        ("rank", "rank"),
        ("name", "name"),
        ("changesets", "changesets"),
        ("map_changes", "map changes"),
        ("nodes_created", "nodes created"),
        ("ways_created", "ways created"),
        ("rels_created", "rels created"),
        ("poi_created", "poi created"),
        ("hashtags", "hashtags"),
    )
    parts.append("| " + " | ".join(label for _, label in user_cols) + " |")
    parts.append("| " + " | ".join("---" for _ in user_cols) + " |")
    for r in rows[:5]:
        cells: list[str] = []
        for key, _ in user_cols:
            v = r.get(key)
            if key == "hashtags":
                hts = v or []
                cells.append(", ".join(hts[:3]) + (f" (+{len(hts) - 3})" if len(hts) > 3 else ""))
            elif key == "name":
                cells.append(str(v or ""))
            elif key == "rank":
                cells.append(str(v if v is not None else ""))
            else:
                cells.append(_human(int(v or 0)))
        parts.append("| " + " | ".join(cells) + " |")

    if tm_stats and any("tasks_mapped" in r for r in rows):
        parts.append("\n#### Top 5 TM mappers")
        for r in sorted(rows, key=lambda x: -(x.get("tasks_mapped", 0) or 0))[:5]:
            parts.append(f"- {r['name']}: {_human(int(r.get('tasks_mapped', 0) or 0))} tasks mapped")

    poi_c = sum(int(r.get("poi_created", 0) or 0) for r in rows)
    poi_m = sum(int(r.get("poi_modified", 0) or 0) for r in rows)
    parts.append(f"\n- poi: created {_human(poi_c)}, modified {_human(poi_m)}")
    for k in additional_tags or []:
        c = sum(int(r.get(f"{k}_create", 0) or 0) for r in rows)
        m = sum(int(r.get(f"{k}_modify", 0) or 0) for r in rows)
        parts.append(f"- {k}: created {_human(c)}, modified {_human(m)}")
    for k in length_tags or []:
        total_m = sum(int(r.get(f"{k}_len_m", 0) or 0) for r in rows)
        parts.append(f"- {k} length created: {_human(round(total_m / 1000))} km")

    if tag_mode != "none":
        merged_create: dict[str, int] = {}
        merged_modify: dict[str, int] = {}
        for r in rows:
            for k, v in (r.get("tags_create") or {}).items():
                merged_create[k] = merged_create.get(k, 0) + int(v)
            for k, v in (r.get("tags_modify") or {}).items():
                merged_modify[k] = merged_modify.get(k, 0) + int(v)
        if merged_create:
            parts.append("\n#### Top 5 created tags")
            for k, v in sorted(merged_create.items(), key=lambda x: -x[1])[:5]:
                parts.append(f"- {k}: {_human(v)}")
        if merged_modify:
            parts.append("\n#### Top 5 modified tags")
            for k, v in sorted(merged_modify.items(), key=lambda x: -x[1])[:5]:
                parts.append(f"- {k}: {_human(v)}")

    for label, col in (("hashtags", "hashtags"), ("editors", "editors")):
        top = _top_n(rows, col)
        if top:
            parts.append(f"\n#### Top 5 {label}")
            for name, count in top:
                parts.append(f"- {name}: {count} users")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return output_path
