"""maintain month completeness guard: never publish a month whose data stops short of its boundary."""

import datetime as dt

import duckdb
import pytest

from osmsg.db.schema import create_tables
from osmsg.exceptions import OsmsgError
from osmsg.maintain.month import verify_month_complete

UTC = dt.UTC


def _db_with_last_changeset(tmp_path, created_at):
    db = tmp_path / "m.duckdb"
    con = duckdb.connect(str(db))
    create_tables(con)
    con.execute(
        "INSERT INTO changesets (changeset_id, uid, created_at) VALUES (1, 1, ?)",
        [created_at],
    )
    con.close()
    return db


def test_complete_month_passes(tmp_path):
    db = _db_with_last_changeset(tmp_path, dt.datetime(2026, 5, 31, 23, 59, tzinfo=UTC))
    verify_month_complete(db, 2026, 5)


def test_truncated_month_raises(tmp_path):
    db = _db_with_last_changeset(tmp_path, dt.datetime(2026, 5, 31, 22, 0, tzinfo=UTC))
    with pytest.raises(OsmsgError, match="incomplete"):
        verify_month_complete(db, 2026, 5)


def test_empty_month_raises(tmp_path):
    db = tmp_path / "e.duckdb"
    con = duckdb.connect(str(db))
    create_tables(con)
    con.close()
    with pytest.raises(OsmsgError):
        verify_month_complete(db, 2026, 5)
