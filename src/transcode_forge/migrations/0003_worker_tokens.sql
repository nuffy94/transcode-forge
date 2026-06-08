-- Worker authentication tokens.
--
-- Each remote worker authenticates over HTTP using a server-issued token
-- (Authorization: Bearer <token>). Tokens are issued from the Settings →
-- Workers UI; revoking a row evicts that worker on its next request.
--
-- worker_id is filled in lazily — on the worker's first /register call
-- the server creates a row in `workers` and links it back here.

CREATE TABLE IF NOT EXISTS worker_tokens (
    token TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    worker_id TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_worker_tokens_worker ON worker_tokens(worker_id);
