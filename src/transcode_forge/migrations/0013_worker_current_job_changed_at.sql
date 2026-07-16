-- Worker-resilience train (plans/worker-resilience-spec.md, D3).
-- workers.current_job_changed_at: when the worker's heartbeat last CHANGED
-- which job it names (including to/from NULL). The reconciliation sweep
-- requeues a live worker's job only on a SUSTAINED mismatch — a single
-- mismatched heartbeat (claim race, report in flight) never costs a worker
-- its job. NULL = no transition observed since this column existed.
ALTER TABLE workers ADD COLUMN current_job_changed_at TEXT;
