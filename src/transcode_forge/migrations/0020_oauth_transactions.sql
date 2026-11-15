-- Authorization transactions for the embedded OAuth server (docs/MCP.md,
-- "Authorization server").
--
-- One row carries one authorization request from start to finish:
-- created when /oauth/authorize validates the request, armed with a nonce
-- when the consent page is rendered, approved when the administrator
-- consents (the grant and the authorization code are recorded on the same
-- row), and consumed when the code is redeemed at /oauth/token. The code
-- is stored only as its kind-prefixed HMAC digest, like every other
-- credential; consumed_at is the single conditional update that makes a
-- code single-use, so a replay has one column to check. consumed_by holds
-- a marker known only to the call that spent the credential: it is read
-- back after the commit, so a consumption that another request's rollback
-- erased on SQLite's shared connection is detected instead of trusted.
--
-- oauth_tokens gains the same marker column for refresh tokens, and
-- oauth_clients the time its metadata document was last fetched, so a
-- client identified by a document URL is re-fetched on a schedule, not on
-- every authorization.

CREATE TABLE oauth_transactions (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES oauth_clients(id),
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT NOT NULL,
    state TEXT,
    nonce_hash TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    approved_at TEXT,
    grant_id TEXT REFERENCES oauth_grants(id),
    code_hash TEXT UNIQUE,
    consumed_at TEXT,
    consumed_by TEXT
);
CREATE INDEX idx_oauth_transactions_expires ON oauth_transactions(expires_at);

ALTER TABLE oauth_tokens ADD COLUMN consumed_by TEXT;
ALTER TABLE oauth_clients ADD COLUMN metadata_fetched_at TEXT;
