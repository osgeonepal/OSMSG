"""CSV exporter using stdlib only."""

import csv
import json
from pathlib import Path
from typing import Any

from .parquet import _priority_key


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def to_csv(rows: list[dict[str, Any]], output_path: Path) -> Path:
    if not rows:
        raise ValueError("nothing to export")
    columns = sorted({k for r in rows for k in r}, key=_priority_key)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _stringify(row.get(c)) for c in columns})
    return output_path
