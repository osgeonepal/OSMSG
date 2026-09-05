"""Export rows as JSON."""

import json
from pathlib import Path
from typing import Any


def to_json(rows: list[dict[str, Any]], output_path: Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rows, default=str, indent=2), encoding="utf-8")
    return output_path
