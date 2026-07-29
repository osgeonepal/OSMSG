-- Rename the tag composite field len_m -> l on an existing Postgres store.
-- Run ONCE before deploying the renamed image (a fresh deploy self-creates the type with `l`).
-- The osmsg_tag composite backs changeset_stats.tags; renaming the attribute keeps existing rows.
ALTER TYPE osmsg_tag RENAME ATTRIBUTE len_m TO l;
