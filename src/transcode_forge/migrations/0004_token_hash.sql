-- Hash worker tokens at rest (M4 Step 13).
--
-- Auth now looks tokens up by HMAC-SHA256(token, pepper), never by the
-- plaintext value. token_prefix is the 6-char fingerprint shown in the UI.
-- expires_at is an optional ISO-8601 UTC expiry (NULL = never).
--
-- The plaintext `token` column is intentionally KEPT (unused by auth) for
-- one release as a rollback escape hatch, then dropped in v0.7.
--
-- Existing rows are backfilled (token_hash/token_prefix computed from the
-- plaintext) by a Python hook in the migration runner keyed to this file's
-- name — HMAC with the pepper can't be expressed portably in pure SQL.

ALTER TABLE worker_tokens ADD COLUMN token_hash TEXT;
ALTER TABLE worker_tokens ADD COLUMN token_prefix TEXT;
ALTER TABLE worker_tokens ADD COLUMN expires_at TEXT;

CREATE INDEX IF NOT EXISTS idx_worker_tokens_hash ON worker_tokens(token_hash);
