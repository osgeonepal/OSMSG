# The native tag breakdown type (osmsg.stats.PG_TAG_TYPE), which the changeset_stats.tags column
# depends on. Created before the schema, only when absent (PG_TAG_TYPE_EXISTS_SQL): a plain CREATE TYPE
# is a recognized write both under asyncpg and DuckDB's postgres_execute, unlike a DO/plpgsql block.
PG_TAG_TYPE_EXISTS_SQL = "SELECT 1 FROM pg_type WHERE typname = 'osmsg_tag'"
PG_TAG_TYPE_SQL = "CREATE TYPE osmsg_tag AS (k text, v text, c bigint, m bigint, len_m double precision)"

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    uid      BIGINT PRIMARY KEY,
    username TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS changesets (
    changeset_id BIGINT PRIMARY KEY,
    uid          BIGINT NOT NULL REFERENCES users(uid),
    created_at   TIMESTAMPTZ,
    hashtags     TEXT[],
    editor       TEXT,
    min_lon      DOUBLE PRECISION,
    min_lat      DOUBLE PRECISION,
    max_lon      DOUBLE PRECISION,
    max_lat      DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_changesets_created_at ON changesets USING BTREE (created_at);
CREATE INDEX IF NOT EXISTS idx_changesets_hashtags ON changesets USING GIN (hashtags);
CREATE INDEX IF NOT EXISTS idx_changesets_editor ON changesets USING BTREE (editor);
CREATE INDEX IF NOT EXISTS idx_changesets_bbox ON changesets USING GIST (
    box(point(min_lon, min_lat), point(max_lon, max_lat))
);
CREATE TABLE IF NOT EXISTS changeset_stats (
    changeset_id   BIGINT NOT NULL REFERENCES changesets(changeset_id),
    seq_id         BIGINT NOT NULL,
    uid            BIGINT NOT NULL REFERENCES users(uid),
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
    tags           osmsg_tag[],
    PRIMARY KEY (seq_id, changeset_id)
);
CREATE INDEX IF NOT EXISTS idx_changeset_stats_uid ON changeset_stats USING BTREE (uid);
CREATE INDEX IF NOT EXISTS idx_changeset_stats_changeset_id ON changeset_stats USING BTREE (changeset_id);
CREATE TABLE IF NOT EXISTS changeset_hashtag (
    hashtag      TEXT NOT NULL,
    changeset_id BIGINT NOT NULL REFERENCES changesets(changeset_id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ,
    PRIMARY KEY (hashtag, changeset_id)
);
CREATE INDEX IF NOT EXISTS idx_changeset_hashtag_lookup ON changeset_hashtag
    USING BTREE (hashtag COLLATE "C", created_at);
CREATE TABLE IF NOT EXISTS state (
    source_url  TEXT PRIMARY KEY,
    last_seq    BIGINT NOT NULL,
    last_ts     TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
);
"""
