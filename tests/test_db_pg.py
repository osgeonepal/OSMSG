from osmsg.db import pg


def test_sql_literal_escapes_and_nulls():
    assert pg.sql_literal(None) == "NULL"
    assert pg.sql_literal("a'b") == "'a''b'"
    assert pg.sql_literal("plain") == "'plain'"


class _RecordingConn:
    def __init__(self):
        self.calls: list[str] = []

    def execute(self, sql: str):
        self.calls.append(sql)


def test_attach_postgres_escapes_dsn_and_sets_flags():
    conn = _RecordingConn()
    pg.attach_postgres(conn, "host=db options='-c work_mem=64MB'", read_only=True)
    assert conn.calls[:2] == ["INSTALL postgres", "LOAD postgres"]
    assert conn.calls[-1] == "ATTACH 'host=db options=''-c work_mem=64MB''' AS pg (TYPE postgres, READ_ONLY)"


def test_attach_postgres_read_write_alias():
    conn = _RecordingConn()
    pg.attach_postgres(conn, "host=db", alias="pg_target")
    assert conn.calls[-1] == "ATTACH 'host=db' AS pg_target (TYPE postgres)"


def test_pg_execute_wraps_in_named_dollar_tag():
    conn = _RecordingConn()
    pg.pg_execute(conn, "DELETE FROM t", alias="pg_target")
    assert conn.calls == ["CALL postgres_execute('pg_target', $osmsg_stmt$DELETE FROM t$osmsg_stmt$)"]
