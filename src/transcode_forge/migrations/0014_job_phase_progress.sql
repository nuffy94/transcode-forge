-- Per-phase progress detail on the job row. Detail-aware workers report
-- a 0-1 fraction (gauge: measured-time / duration) and/or a short label
-- (search: "q3/5" probe count) with each progress POST; the station bar
-- renders it as a suffix on the active station. NULL = pre-detail worker
-- or a phase with nothing to report (UI renders exactly as before).
ALTER TABLE jobs ADD COLUMN phase_pct REAL;
ALTER TABLE jobs ADD COLUMN phase_detail TEXT;
