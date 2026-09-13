-- Attempt identity (ledger R-020, and R-012's derivative shortcut).
-- jobs.claim_token: a fresh opaque value stamped by every claim and cleared
-- by every release. Job ids and worker ids are both reused across attempts,
-- so neither can tell the scheduler WHICH execution of a job a worker report
-- speaks for; the token can. A report is accepted only while the token it
-- carries still matches the row, so a report parked in a worker's outbox
-- during attempt 1 can never finalize attempt 2. NULL = the job is not owned
-- (or was claimed before this column existed).
-- workers.sends_claim_token: advertised at registration, exactly like
-- supports_downscale. A worker that advertised it and then reports without a
-- token is refused; one that never advertised is judged by worker id alone,
-- which is what keeps a rolling update working.
ALTER TABLE jobs ADD COLUMN claim_token TEXT;
ALTER TABLE workers ADD COLUMN sends_claim_token INTEGER NOT NULL DEFAULT 0;
