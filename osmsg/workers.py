"""ProcessPoolExecutor entry points for changeset + changefile parsing."""

import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from .db.ingest import flush_rows_to_parquet
from .fetch import file_path_for
from .handlers import ChangefileHandler, ChangesetHandler


def _warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


_VALID_CHANGESETS: set[int] | None = None
_CS_CONFIG: dict[str, Any] | None = None
_CF_CONFIG: dict[str, Any] | None = None
_BATCH_COUNTER: int = 0


def init_changeset_worker(config: dict[str, Any]) -> None:
    global _CS_CONFIG, _BATCH_COUNTER
    _CS_CONFIG = config
    _BATCH_COUNTER = 0


def init_changefile_worker(valid_changesets: set[int] | None, config: dict[str, Any]) -> None:
    global _VALID_CHANGESETS, _CF_CONFIG, _BATCH_COUNTER
    _VALID_CHANGESETS = valid_changesets
    _CF_CONFIG = config
    _BATCH_COUNTER = 0


def _next_batch() -> int:
    global _BATCH_COUNTER
    _BATCH_COUNTER += 1
    return _BATCH_COUNTER


def process_changeset(url: str) -> None:
    cfg = _CS_CONFIG
    if cfg is None:
        raise RuntimeError("init_changeset_worker must run first")

    raw_path = file_path_for(url, "changeset", Path(cfg["cache_dir"])).with_suffix("")
    handler = ChangesetHandler(cfg)
    try:
        handler.apply_file(str(raw_path))
    except Exception as exc:
        _warn(f"changeset file may be corrupt ({url}): {exc}")

    flush_rows_to_parquet(
        parquet_dir=Path(cfg["parquet_dir"]),
        pid=os.getpid(),
        batch_index=_next_batch(),
        users=[u.to_row() for u in handler.users.values()],
        changesets=[c.to_row() for c in handler.changesets.values()],
    )

    if cfg.get("delete_temp"):
        with suppress(OSError):
            raw_path.unlink()


def process_changefile(url: str, sequence_id: int) -> None:
    cfg = _CF_CONFIG
    if cfg is None:
        raise RuntimeError("init_changefile_worker must run first")

    raw_path = file_path_for(url, "changefiles", Path(cfg["cache_dir"])).with_suffix("")

    handler = ChangefileHandler(cfg, sequence_id, _VALID_CHANGESETS)
    try:
        # locations=True so open ways can be measured (haversine needs node coords); length is always on.
        handler.apply_file(str(raw_path), locations=True)
    except Exception as exc:
        _warn(f"changefile may be corrupt ({url}): {exc}")

    flush_rows_to_parquet(
        parquet_dir=Path(cfg["parquet_dir"]),
        pid=os.getpid(),
        batch_index=_next_batch(),
        users=[u.to_row() for u in handler.users.values()],
        changesets=[c.to_row() for c in handler.stubs.values()],
        changeset_stats=[s.to_row() for s in handler.stats.values()],
    )

    if cfg.get("delete_temp"):
        with suppress(OSError):
            raw_path.unlink()
