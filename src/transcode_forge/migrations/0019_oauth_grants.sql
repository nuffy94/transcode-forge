-- Grant store for the MCP endpoint (docs/MCP.md, "Grants and tokens").
--
-- A grant is one consent decision: one client, one user, a scope set, the
-- resource the tokens are minted for, a deadline and a revocation stamp.
-- Every credential (access token, refresh token, authorization code) hangs
-- off a grant, so revoking the grant ends all of them at their next check.
-- Tokens are stored only as kind-prefixed HMAC-SHA256 digests; the raw
-- value is never written.
--
-- Timestamps are ISO-8601 UTC text, matching worker_tokens.

CREATE TABLE oauth_clients (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL DEFAULT '[]',
    metadata_url TEXT,
    created_at TEXT NOT NULL,
    disabled_at TEXT
);

CREATE TABLE oauth_grants (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES oauth_clients(id),
    user_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT
);
CREATE INDEX idx_oauth_grants_client ON oauth_grants(client_id);

CREATE TABLE oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES oauth_grants(id),
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX idx_oauth_tokens_grant ON oauth_tokens(grant_id);
