"""Single-row resume state per source_url, replaces the old per-run audit log."""

from __future__ import annotations

import datetime as dt

from osmsg.db.schema import create_tables, get_state, upsert_state


def test_get_state_returns_none_for_unseen_source(fresh_db):
    assert get_state(fresh_db, "https://example.com/replication") is None


def test_upsert_state_inserts_on_first_call(fresh_db):
    upsert_state(
        fresh_db,
        source_url="https://example.com/replication",
        last_seq=42,
        last_ts=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 4, 25, 12, 0, 30, tzinfo=dt.UTC),
    )
    s = get_state(fresh_db, "https://example.com/replication")
    assert s is not None
    assert s["last_seq"] == 42
    assert s["last_ts"] == dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.UTC)


def test_upsert_state_replaces_on_second_call(fresh_db):
    """Same source_url + new seq → exactly one row remains, not two."""
    for seq in (10, 20, 30):
        upsert_state(
            fresh_db,
            source_url="X",
            last_seq=seq,
            last_ts=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.UTC),
            updated_at=dt.datetime.now(dt.UTC),
        )

    rows = fresh_db.execute("SELECT COUNT(*) FROM state WHERE source_url = 'X'").fetchone()
    assert rows[0] == 1, "state must hold exactly one row per source_url"
    assert get_state(fresh_db, "X")["last_seq"] == 30


def test_upsert_state_independent_per_source_url(fresh_db):
    upsert_state(
        fresh_db,
        source_url="A",
        last_seq=1,
        last_ts=dt.datetime(2026, 4, 25, tzinfo=dt.UTC),
        updated_at=dt.datetime.now(dt.UTC),
    )
    upsert_state(
        fresh_db,
        source_url="B",
        last_seq=99,
        last_ts=dt.datetime(2026, 4, 25, tzinfo=dt.UTC),
        updated_at=dt.datetime.now(dt.UTC),
    )

    assert get_state(fresh_db, "A")["last_seq"] == 1
    assert get_state(fresh_db, "B")["last_seq"] == 99
    rows = fresh_db.execute("SELECT COUNT(*) FROM state").fetchone()[0]
    assert rows == 2


def test_create_tables_is_idempotent(tmp_path):
    """create_tables() must be safe to call repeatedly, that's how --update opens existing DBs."""
    import duckdb

    conn = duckdb.connect(str(tmp_path / "x.duckdb"))
    create_tables(conn)
    create_tables(conn)
    tables = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert {"users", "changesets", "changeset_stats", "state"}.issubset(tables)


def test_state_resume_signal_consumed_by_update_flow(fresh_db):
    """The exact value cfg.start_date reads when --update is on."""
    upsert_state(
        fresh_db,
        source_url="https://download.geofabrik.de/asia/nepal-updates",
        last_seq=4289,
        last_ts=dt.datetime(2026, 4, 25, 11, 59, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.UTC),
    )

    s = get_state(fresh_db, "https://download.geofabrik.de/asia/nepal-updates")
    assert s["last_ts"] == dt.datetime(2026, 4, 25, 11, 59, tzinfo=dt.UTC)
