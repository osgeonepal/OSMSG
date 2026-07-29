# No FKs: DuckDB rejects UPDATE on FK-referenced LIST/GEOMETRY columns, which would block changeset upgrades.
# `tags` is the native tag breakdown (osmsg.stats.TAG_STRUCT_DDL); kept in sync with that constant.
DUCKDB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    uid      BIGINT PRIMARY KEY,
    username VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS changesets (
    changeset_id BIGINT PRIMARY KEY,
    uid          BIGINT NOT NULL,
    created_at   TIMESTAMPTZ,
    hashtags     VARCHAR[],
    editor       VARCHAR,
    geom         GEOMETRY
);
CREATE INDEX IF NOT EXISTS idx_changesets_created_at ON changesets(created_at);
CREATE TABLE IF NOT EXISTS changeset_stats (
    changeset_id   BIGINT NOT NULL,
    seq_id         BIGINT NOT NULL,
    uid            BIGINT NOT NULL,
    nodes_created  INTEGER DEFAULT 0,
    nodes_modified INTEGER DEFAULT 0,
    nodes_deleted  INTEGER DEFAULT 0,
    ways_created   INTEGER DEFAULT 0,
    ways_modified  INTEGER DEFAULT 0,
    ways_deleted   INTEGER DEFAULT 0,
    rels_created   INTEGER DEFAULT 0,
    rels_modified  INTEGER DEFAULT 0,
    rels_deleted   INTEGER DEFAULT 0,
    poi_created    INTEGER DEFAULT 0,
    poi_modified   INTEGER DEFAULT 0,
    tags           STRUCT(k VARCHAR, v VARCHAR, c BIGINT, m BIGINT, l DOUBLE)[],
    PRIMARY KEY (seq_id, changeset_id)
);
CREATE INDEX IF NOT EXISTS idx_changeset_stats_uid ON changeset_stats(uid);
CREATE TABLE IF NOT EXISTS state (
    source_url  VARCHAR PRIMARY KEY,
    last_seq    BIGINT NOT NULL,
    last_ts     TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
);
"""
