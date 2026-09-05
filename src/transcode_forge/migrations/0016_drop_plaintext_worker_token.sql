-- Drop the plaintext worker token column.
--
-- 0003 created worker_tokens keyed on the raw bearer token. 0004 added
-- token_hash and token_prefix, switched auth to the hash, and said the
-- plaintext column would go one release later. That never happened.
-- create() kept writing the raw token next to its hash, so a copy of the
-- database contained working bearer tokens. Nothing has read the column
-- since 0004.
--
-- SQLite cannot DROP COLUMN on a primary key column, so this is a rebuild:
-- new table keyed on token_hash, copy the rows, drop the old table, rename
-- the new one into place. The same statements run on Postgres.
--
-- Nothing else in the schema points at worker_tokens (no foreign key,
-- view or trigger), so DROP TABLE and RENAME are safe under the production
-- PRAGMA foreign_keys=ON and do not depend on legacy_alter_table.
--
-- Only rows with a hash are copied. A row without one cannot authenticate
-- (auth matches on token_hash only).
--
-- Indexes: idx_worker_tokens_hash (0004) is dropped. The primary key
-- covers that lookup. uq_worker_tokens_worker_id (0010) is recreated on
-- the new table. DROP TABLE removes both on either dialect; the explicit
-- DROP INDEX makes the final state visible in this file.

CREATE TABLE worker_tokens_new (
    token_hash TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    worker_id TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT,
    token_prefix TEXT,
    expires_at TEXT
);

INSERT INTO worker_tokens_new
    (token_hash, label, worker_id, created_at, revoked_at, last_used_at, token_prefix, expires_at)
SELECT token_hash, label, worker_id, created_at, revoked_at, last_used_at, token_prefix, expires_at
FROM worker_tokens
WHERE token_hash IS NOT NULL;

DROP INDEX IF EXISTS idx_worker_tokens_hash;

DROP TABLE worker_tokens;

ALTER TABLE worker_tokens_new RENAME TO worker_tokens;

CREATE UNIQUE INDEX uq_worker_tokens_worker_id ON worker_tokens(worker_id);
