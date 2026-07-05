-- One worker identity per token, enforced at the schema level
-- (adversarial review item 16: worker-token reuse race).
--
-- Registration binds token -> worker via an atomic conditional UPDATE
-- (repos/worker_tokens.link_worker); this unique index is the backstop so
-- no code path can ever leave two token rows pointing at the same worker
-- identity.
--
-- SQLite cannot ALTER TABLE ... ADD CONSTRAINT, so a UNIQUE INDEX is used
-- instead — semantically equivalent and valid on both dialects. Multiple
-- NULLs (tokens issued but never registered) are permitted by both SQLite
-- and Postgres unique-index semantics. Duplicate non-NULL worker_ids
-- cannot exist in released installs: link_worker only ever writes a
-- freshly generated UUID into a single token row, so no cleanup pass is
-- needed before creating the index.
--
-- The plain index from 0003 is superseded by the unique one (same column,
-- same lookups) and dropped here.

DROP INDEX IF EXISTS idx_worker_tokens_worker;

CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_tokens_worker_id
    ON worker_tokens(worker_id);
