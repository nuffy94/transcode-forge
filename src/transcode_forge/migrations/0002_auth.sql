-- v0.5 admin auth.
--
-- Single-admin model — one row in users. First-run UX: the setup page
-- prompts for a password if no admin row exists yet.
--
-- The auth secret used to sign session cookies is generated lazily and
-- stored in system_state under the key 'auth_secret'. We don't pre-seed
-- it here because we want each install to get its own.

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
