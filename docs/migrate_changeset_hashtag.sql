-- One-time migration: the changeset_hashtag lookup table.
--
-- It lets the API answer a hashtag-prefix query (e.g. everything under "hotosm") by an index range
-- scan instead of scanning the whole recent Postgres tail. One lowercased (hashtag, changeset_id) row
-- per tag, indexed by (hashtag COLLATE "C", created_at): COLLATE "C" is byte ordering, which matches
-- how osmsg computes a prefix's exclusive upper bound, so `hashtag >= '#x' AND hashtag < '#y'` prunes.
--
-- A fresh deployment self-creates this table via the app schema, but an existing Postgres still needs
-- the backfill, and any table created before this migration (e.g. a hand-built one without the primary
-- key) must be normalised. This script drops and rebuilds from `changesets` (the source of truth), in
-- one transaction, so it is safe to re-run and leaves the table in the canonical shape either way.
--
-- Run it once BEFORE deploying the image that queries changeset_hashtag.

BEGIN;

DROP TRIGGER IF EXISTS osmsg_ch_sync_trg ON changesets;
DROP TABLE IF EXISTS changeset_hashtag;

CREATE TABLE changeset_hashtag (
    hashtag      TEXT NOT NULL,
    changeset_id BIGINT NOT NULL REFERENCES changesets(changeset_id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ,
    PRIMARY KEY (hashtag, changeset_id)
);

INSERT INTO changeset_hashtag (hashtag, changeset_id, created_at)
SELECT lower(h), c.changeset_id, c.created_at
FROM changesets c, unnest(c.hashtags) AS h
ON CONFLICT (hashtag, changeset_id) DO NOTHING;

CREATE INDEX idx_changeset_hashtag_lookup
    ON changeset_hashtag USING BTREE (hashtag COLLATE "C", created_at);

ANALYZE changeset_hashtag;

COMMIT;

-- Optional interim shim. Keeps changeset_hashtag current while a WORKER image that predates this
-- feature is still running (a current worker fills it in `to_psql`, making the trigger redundant, so
-- it is safe to keep or DROP once the worker image is updated). Uncomment to install.
--
-- CREATE OR REPLACE FUNCTION osmsg_ch_sync() RETURNS trigger AS $fn$
-- BEGIN
--     INSERT INTO changeset_hashtag (hashtag, changeset_id, created_at)
--     SELECT lower(h), NEW.changeset_id, NEW.created_at FROM unnest(NEW.hashtags) AS h
--     ON CONFLICT (hashtag, changeset_id) DO NOTHING;
--     RETURN NEW;
-- END $fn$ LANGUAGE plpgsql;
-- CREATE TRIGGER osmsg_ch_sync_trg AFTER INSERT ON changesets
--     FOR EACH ROW EXECUTE FUNCTION osmsg_ch_sync();
